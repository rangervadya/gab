"""Deployment tests use an in-memory Docker Engine; no production commands run."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from deploy import Deployment, fingerprint
from deploy_runtime import ContainerSwap, DeployError, clone_payload
from workflow_validation import read_workflow, validate_workflow, WORKFLOW_ID


def container():
    return {'Id':'old-id','Name':'/gab-review','Image':'sha256:previous',
        'State':{'Running':True},
        'Config':{'Image':'gab-review:old','Env':['GAB_APP_PASSWORD=synthetic','DATABASE_URL=synthetic'],
            'Cmd':['uvicorn','app.main:app','--port','8000'], 'Entrypoint':None,
            'WorkingDir':'/srv/gab-review','User':'123:456','Labels':None,
            'Volumes':{'/data':{}},'Healthcheck':{'Test':['CMD','true']}},
        'HostConfig':{'Binds':['/opt/gab-review-data:/srv/gab-review-data:rw'],
            'PortBindings':{'8000/tcp':[{'HostIp':'192.168.1.99','HostPort':'8091'}]},
            'NetworkMode':'ai-stack_ai_net','RestartPolicy':{'Name':'unless-stopped'},
            'Memory':123456789,'ReadonlyRootfs':True,'CapDrop':['ALL'],
            'LogConfig':{'Type':'json-file','Config':{'max-size':'10m'}}},
        'Mounts':[{'Type':'bind','Source':'/opt/gab-review-data','Destination':'/srv/gab-review-data','RW':True},
                  {'Type':'volume','Name':'old-anonymous-volume','Destination':'/data','RW':True}],
        'NetworkSettings':{'Networks':{'ai-stack_ai_net':{'IPAMConfig':{'IPv4Address':'172.19.0.30'},
            'Aliases':['gab-review','custom-crm'],'IPAddress':'172.19.0.30','EndpointID':'old-endpoint'},
            'second-network':{'IPAMConfig':None,'Aliases':['other-alias'],'IPAddress':'172.20.0.3'}}}}


class FakeDocker:
    def __init__(self, fail_create=False):
        self.containers={'old-id':container()}
        self.calls=[]
        self.fail_create=fail_create

    def resolve(self, value):
        for key,data in self.containers.items():
            if key==value or data['Name'].lstrip('/')==value:return key
        raise DeployError('not found')

    def run(self,args,**kwargs):
        self.calls.append(list(args))
        command=args[1]
        key=self.resolve(args[-1]) if command in {'inspect','stop','start','rm'} else None
        if command=='inspect':return json.dumps([self.containers[key]]).encode()
        if command=='stop':self.containers[key]['State']['Running']=False
        elif command=='start':self.containers[key]['State']['Running']=True
        elif command=='rename':self.containers[self.resolve(args[2])]['Name']='/'+args[3]
        elif command=='rm':del self.containers[key]
        else:raise AssertionError(args)
        return b''

    def json(self,args):return json.loads(self.run(args))

    def request(self,method,path,body):
        self.calls.append([method,path,copy.deepcopy(body)])
        if path.startswith('/containers/create'):
            if self.fail_create:raise DeployError('injected create failure')
            self.containers['new-id']={'Id':'new-id','Name':'/gab-review','Image':body['Image'],
                'State':{'Running':False},'Config':{k:v for k,v in body.items() if k not in {'HostConfig','NetworkingConfig'}},
                'HostConfig':body['HostConfig'],'Mounts':copy.deepcopy(container()['Mounts']),'NetworkSettings':{'Networks':copy.deepcopy(body['NetworkingConfig']['EndpointsConfig'])}}
            return {'Id':'new-id'}
        name=path.split('/')[2]
        if path.endswith('/disconnect'):
            self.containers[self.resolve(body['Container'])]['NetworkSettings']['Networks'].pop(name,None)
        elif path.endswith('/connect'):
            self.containers[self.resolve(body['Container'])]['NetworkSettings']['Networks'][name]=body['EndpointConfig']
        else:raise AssertionError(path)


class ContainerTests(unittest.TestCase):
    def test_clone_preserves_runtime_configuration(self):
        original=container();payload=clone_payload(original,'gab-review:new')
        for key,value in original['Config'].items():
            self.assertEqual(payload[key], 'gab-review:new' if key=='Image' else value)
        for key,value in original['HostConfig'].items():self.assertEqual(payload['HostConfig'][key],value)
        self.assertEqual(payload['HostConfig']['Mounts'][0]['Source'],'old-anonymous-volume')
        net=payload['NetworkingConfig']['EndpointsConfig']['ai-stack_ai_net']
        self.assertEqual(net['IPAMConfig'],{'IPv4Address':'172.19.0.30'})
        self.assertNotIn('EndpointID',net)
        self.assertNotIn('IPAddress',net)
        self.assertNotIn('Mounts',original['HostConfig'])

    def test_replace_and_restore_all_networks_and_original(self):
        docker=FakeDocker();journal=[]
        swap=ContainerSwap(docker,docker,journal,lambda:None)
        snapshot=copy.deepcopy(docker.containers['old-id'])
        swap.replace(snapshot,'gab-review:new','synthetic-release')
        self.assertFalse(docker.containers['old-id']['State']['Running'])
        self.assertTrue(docker.containers['new-id']['State']['Running'])
        swap.restore(journal[0],'synthetic-release')
        self.assertEqual(set(docker.containers),{'old-id'})
        old=docker.containers['old-id']
        self.assertEqual(old['Name'],'/gab-review')
        self.assertTrue(old['State']['Running'])
        self.assertEqual(set(old['NetworkSettings']['Networks']),set(snapshot['NetworkSettings']['Networks']))
        self.assertEqual(old['HostConfig'],snapshot['HostConfig'])
        self.assertFalse(any('-v' in call for call in docker.calls))

    def test_create_failure_can_restore_detached_old_container(self):
        docker=FakeDocker(fail_create=True);journal=[]
        swap=ContainerSwap(docker,docker,journal,lambda:None)
        with self.assertRaises(DeployError):swap.replace(container(),'new-image','release')
        swap.restore(journal[0],'release')
        self.assertTrue(docker.containers['old-id']['State']['Running'])
        self.assertEqual(docker.containers['old-id']['Name'],'/gab-review')
        self.assertEqual(len(docker.containers['old-id']['NetworkSettings']['Networks']),2)

    def test_refuse_to_remove_unrelated_container(self):
        docker=FakeDocker();journal=[];swap=ContainerSwap(docker,docker,journal,lambda:None)
        swap.replace(container(),'new-image','release')
        docker.containers['new-id']['Config']['Labels']['gab.deploy.release']='another-release'
        with self.assertRaises(DeployError):swap.restore(journal[0],'release')
        self.assertIn('new-id',docker.containers)


class WorkflowValidationTests(unittest.TestCase):
    def setUp(self):self.workflow=read_workflow(SCRIPTS.parent/'n8n/workflows/GAB02InvImp20261.json')

    def test_real_export_all_27_nodes_and_branches(self):
        self.assertEqual(len(validate_workflow(self.workflow)['nodes']),27)

    def test_reject_mail_node_even_with_27_nodes(self):
        self.workflow['nodes'][0]['type']='n8n-nodes-base.emailSend'
        with self.assertRaises(ValueError):validate_workflow(self.workflow)

    def test_reject_changed_credential(self):
        node=next(n for n in self.workflow['nodes'] if n['name']=='OCR PDF')
        node['credentials']['httpHeaderAuth']['id']='incorrect'
        with self.assertRaises(ValueError):validate_workflow(self.workflow)

    def test_reject_missing_excel_branch(self):
        self.workflow['connections']['Extract XLSX']['main'][0]=[]
        with self.assertRaises(ValueError):validate_workflow(self.workflow)

    def test_reject_automatic_confirmation(self):
        node=next(n for n in self.workflow['nodes'] if n['name']=='Save PDF records')
        node['parameters']['url']=node['parameters']['url'].replace('/rows','/confirm')
        with self.assertRaises(ValueError):validate_workflow(self.workflow)

    def test_reject_missing_worker_token(self):
        node=next(n for n in self.workflow['nodes'] if n['name']=='Save spreadsheet records')
        node['parameters']['jsonBody']='={{ JSON.stringify({records:$json.records}) }}'
        with self.assertRaises(ValueError):validate_workflow(self.workflow)

    def test_fingerprint_detects_other_workflow_change_only(self):
        other={'id':'mailings','nodes':[{'type':'synthetic'}],'active':True}
        before=fingerprint([self.workflow,other]);self.workflow['name']='changed target'
        self.assertEqual(before,fingerprint([self.workflow,other]))
        other['active']=False
        self.assertNotEqual(before,fingerprint([self.workflow,other]))


class Lifecycle(Deployment):
    """Exercise real execute() ordering and failure compensation, without I/O."""
    def __init__(self, tmp, failure=None):
        super().__init__(config={'state_dir':tmp},runner=object(),engine=object())
        self.state['commit']='synthetic-commit';self.previous={'active':False}
        self.events=[];self.failure=failure
    def event(self,name):
        self.events.append(name)
        if self.failure==name:raise DeployError('injected '+name)
    def backup(self):self.event('backup');self.state['backup']='encrypted-verified.tar.age'
    def build_crm(self):self.event('build');self.state['new_crm_image']='synthetic'
    def quiesce(self):self.state['workflow_touched']=True;self.event('quiesce')
    def migrate(self):self.event('migration');self.state['migration_applied']=True
    def update_crm(self):self.state['swaps'].append({'synthetic':True});self.event('crm')
    def update_ocr(self):self.event('ocr')
    def publish(self):self.event('publish')
    def assert_other_workflows(self):self.event('other-workflows')
    def rollback(self):self.events.append('rollback');self.state['status']='rolled-back';self.save()


class LifecycleTests(unittest.TestCase):
    def test_order_backup_before_every_production_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployment=Lifecycle(tmp);deployment.execute()
            self.assertEqual(deployment.events,['backup','build','quiesce','migration','crm','ocr','publish','other-workflows'])
            self.assertEqual(deployment.state['status'],'completed')

    def test_all_post_quiesce_failures_propagate_and_trigger_rollback(self):
        for stage in ('quiesce','migration','crm','ocr','publish','other-workflows'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as tmp:
                d=Lifecycle(tmp,stage)
                with self.assertRaises(DeployError):d.execute()
                self.assertEqual(d.events[-1],'rollback')
                self.assertEqual(d.state['status'],'rolled-back')

    def test_backup_and_build_failure_leave_production_untouched(self):
        for stage in ('backup','build'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as tmp:
                d=Lifecycle(tmp,stage)
                with self.assertRaises(DeployError):d.execute()
                self.assertNotIn('quiesce',d.events)
                self.assertNotIn('migration',d.events)
                self.assertNotIn('rollback',d.events)

    def test_backup_component_never_changes_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Lifecycle(tmp);d.execute('backup')
            self.assertEqual(d.events,['backup','other-workflows'])


class PublicationTests(unittest.TestCase):
    def deployment(self):
        d=Deployment(config={'publication_timeout_seconds':30},runner=object(),engine=object())
        d.workflow={'nodes':[{'id':'schedule','type':'n8n-nodes-base.scheduleTrigger'}]}
        d.workflow_state=lambda:{'activeVersionId':'synthetic-version'}
        return d

    def test_database_active_flag_is_not_enough(self):
        d=self.deployment()
        statuses=iter([
            {'published_version':None,'in_flight':1,'triggers':[]},
            {'published_version':'synthetic-version','in_flight':0,
             'triggers':[{'nodeId':'schedule','versionId':'synthetic-version','status':'activated'}]}])
        d.publication_status=lambda:next(statuses)
        with patch('deploy.time.sleep') as sleep:
            d.wait_for_publication('synthetic-version')
        sleep.assert_called_once_with(3)

    def test_failed_trigger_is_not_reported_as_success(self):
        d=self.deployment()
        d.publication_status=lambda:{'published_version':'synthetic-version','in_flight':0,
            'triggers':[{'nodeId':'schedule','versionId':'synthetic-version','status':'failed'}]}
        with self.assertRaises(DeployError):d.wait_for_publication('synthetic-version')

    def test_unpublish_waits_for_runtime_version_removal(self):
        d=self.deployment();d.workflow_state=lambda:{'activeVersionId':None}
        statuses=iter([{'published_version':'old-version','in_flight':0,'triggers':[]},
                       {'published_version':None,'in_flight':0,'triggers':[]}])
        d.publication_status=lambda:next(statuses)
        with patch('deploy.time.sleep') as sleep:d.wait_for_publication()
        sleep.assert_called_once_with(3)

    def test_inactive_workflow_does_not_wait_for_unrelated_jobs(self):
        d=self.deployment();d.workflow_state=lambda:{'activeVersionId':None}
        d.publication_status=lambda:{'published_version':None,'in_flight':0,'triggers':[]}
        with patch('deploy.time.sleep') as sleep:d.wait_for_publication()
        sleep.assert_not_called()


if __name__=='__main__':unittest.main()

#!/usr/bin/env python3
"""GAB office deployment. --check is read-only; no production execution in tests."""
import argparse
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from deploy_config import ROOT, load_config
from deploy_runtime import (ContainerSwap, DeployError, Engine, Runner, environment,
                            health_check, write_json)
from workflow_validation import WORKFLOW_ID, read_workflow, validate_workflow


def log(message):
    print(message, flush=True)


def fingerprint(workflows):
    # Exclude counters and update timestamps; detect code, publication and
    # credential-reference changes in all other workflows without changing them.
    return {w['id']:hashlib.sha256(json.dumps({key:w.get(key) for key in
        ('name','nodes','connections','settings','active','versionId','activeVersionId')},
        sort_keys=True).encode()).hexdigest() for w in workflows if w['id'] != WORKFLOW_ID}


class Deployment:
    def __init__(self, config=None, runner=None, engine=None):
        self.config = config if config is not None else load_config()
        self.runner = runner or Runner()
        self.engine = engine
        self.state = {'swaps':[], 'workflow_touched':False, 'status':'preflight'}
        self.state_path = None
        self.previous = None
        self.all_workflows = None
        self.snapshots = {}
        self.remote_files = []
        self.rebuild_ocr = False

    def save(self):
        if self.state_path:
            write_json(self.state_path, self.state)

    def inspect(self, name):
        return self.runner.json(['docker','inspect',name])[0]

    def compose(self, *args):
        return ['docker','compose','-f',self.config['compose_file'],*args]

    def n8n(self, *args):
        return self.runner.run(['docker','exec','--user','node',self.config['n8n_container'],'n8n',*args])

    def export_workflows(self):
        # n8n prints log lines around exports, so always export to a file.
        remote = '/tmp/gab-export-'+uuid.uuid4().hex+'.json'
        self.remote_files.append(remote)
        self.n8n('export:workflow','--all','--output='+remote)
        raw = self.runner.run(['docker','exec',self.config['n8n_container'],'cat',remote])
        self.runner.run(['docker','exec',self.config['n8n_container'],'rm','-f',remote])
        self.remote_files.remove(remote)
        workflows = json.loads(raw)
        if not isinstance(workflows, list):
            raise DeployError('Unexpected n8n export format')
        return workflows

    def sql(self, query, *, database=None, data=None):
        args = ['docker','exec','-i',self.config['postgres_container'],'psql','-X',
                '-v','ON_ERROR_STOP=1','-U',self.config['postgres_user'],'-d',
                database or self.config['database']]
        if query is not None:
            args += ['-Atc',query]
        return self.runner.run(args,data=data).decode().strip()

    def n8n_database(self):
        env = environment(self.snapshots['n8n'])
        if env.get('DB_TYPE') != 'postgresdb':
            raise DeployError('This audited deployment expects PostgreSQL-backed n8n')
        name = env.get('DB_POSTGRESDB_DATABASE')
        if not name or name == self.config['database']:
            raise DeployError('CRM and n8n must use separate databases')
        return name

    def workflow_state(self):
        raw = self.sql('SELECT row_to_json(w) FROM (SELECT id, active, "versionId", "activeVersionId" '
            'FROM workflow_entity WHERE id=\''+WORKFLOW_ID+'\') w', database=self.n8n_database())
        if not raw:
            raise DeployError('GAB 02 does not exist in n8n')
        return json.loads(raw)

    def publication_status(self):
        query = "SELECT json_build_object(" \
            "'published_version',(SELECT \"publishedVersionId\" FROM workflow_published_version WHERE \"workflowId\"='"+WORKFLOW_ID+"')," \
            "'in_flight',(SELECT count(*) FROM workflow_publication_outbox WHERE \"workflowId\"='"+WORKFLOW_ID+"' AND status IN ('pending','in_progress'))," \
            "'triggers',COALESCE((SELECT json_agg(row_to_json(t)) FROM (SELECT \"nodeId\",\"versionId\",status FROM workflow_publication_trigger_status WHERE \"workflowId\"='"+WORKFLOW_ID+"') t),'[]'::json))"
        return json.loads(self.sql(query,database=self.n8n_database()))

    def wait_for_publication(self, version=None, workflow=None):
        deadline=time.monotonic()+int(self.config['publication_timeout_seconds'])
        workflow=workflow or getattr(self,'workflow', {'nodes':[]})
        expected={n['id'] for n in workflow['nodes'] if n['type']=='n8n-nodes-base.scheduleTrigger' and not n.get('disabled')}
        while True:
            desired=self.workflow_state().get('activeVersionId')
            status=self.publication_status()
            if version is None:
                settled=desired is None and status['published_version'] is None and status['in_flight']==0
            else:
                rows=[r for r in status['triggers'] if r['versionId']==version]
                if any(r['status']=='failed' for r in rows):
                    raise DeployError('GAB 02 trigger activation failed; no n8n restart will be attempted')
                active={r['nodeId'] for r in rows if r['status']=='activated'}
                settled=desired==version and status['published_version']==version and status['in_flight']==0 and expected <= active
            if settled:return
            if time.monotonic()>=deadline:
                raise DeployError('Running n8n did not apply GAB 02 publication in time')
            time.sleep(3)

    def verify_mail_sender(self):
        from mailing_validation import credential_document,sender_references
        env=environment(self.snapshots['crm'])
        url=env.get('GAB_MAILING_WEBHOOK_URL','')
        token=env.get('GAB_MAILING_WEBHOOK_TOKEN','')
        from urllib.parse import urlsplit
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!='n8n.techsmysl.ru' or not token:
            raise DeployError('Почтовый webhook CRM не настроен')
        path=parsed.path.removeprefix('/webhook/').strip('/')
        matches=[w for w in self.all_workflows if any(n.get('type')=='n8n-nodes-base.webhook' and n.get('parameters',{}).get('path','').strip('/')==path for n in w.get('nodes',[]))]
        if len(matches)!=1:raise DeployError('Не найден единственный почтовый workflow')
        remote='/tmp/gab-mail-contract-'+uuid.uuid4().hex+'.json';self.remote_files.append(remote)
        self.n8n('export:workflow','--id='+matches[0]['id'],'--published','--output='+remote)
        try:
            published=json.loads(self.runner.run(['docker','exec',self.config['n8n_container'],'cat',remote]))[0]
        finally:
            self.runner.run(['docker','exec',self.config['n8n_container'],'rm','-f',remote]);self.remote_files.remove(remote)
        published['activeVersionId']=matches[0].get('activeVersionId')
        header_id,smtp_id=sender_references(published,url)
        # Compare the shared token only in memory, never in files, arguments or
        # output. SMTP credentials remain encrypted and are not used to log in.
        header=credential_document(self.n8n('export:credentials','--id='+header_id,'--decrypted'),header_id)
        data=header['data']
        if isinstance(data,str):data=json.loads(data)
        import hmac
        if data.get('name','').lower()!='x-gab-webhook-token' or not hmac.compare_digest(str(data.get('value','')),token):
            raise DeployError('Почтовый webhook token CRM не совпадает с Header Auth n8n')
        smtp=credential_document(self.n8n('export:credentials','--id='+smtp_id),smtp_id)
        if smtp.get('type')!='smtp':raise DeployError('SMTP credential не найден')
        log('Почтовый workflow и общий token проверены. SMTP/webhook не вызывались; писем отправлено: 0.')

    def assert_no_mail_dispatch(self):
        if self.sql("SELECT to_regclass('public.gab_crm_mailing_runs')"):
            if self.sql("SELECT count(*) FROM gab_crm_mailing_runs WHERE status='running'")!='0':
                raise DeployError('Есть незавершённая почтовая отправка. Обновление/откат CRM запрещён до её проверки')

    def assert_idle(self):
        busy = self.sql('SELECT count(*) FROM execution_entity WHERE "workflowId"=\''+WORKFLOW_ID+
            "' AND status IN ('new','running','waiting')", database=self.n8n_database())
        processing = self.sql("SELECT count(*) FROM gab_crm_import_batches WHERE status='processing'")
        if busy != '0' or processing != '0':
            raise DeployError('GAB 02 has unfinished executions or processing batches; finish/reconcile them first')

    def revision_checks(self):
        branch = self.runner.run(['git','-C',ROOT,'branch','--show-current']).decode().strip()
        dirty = self.runner.run(['git','-C',ROOT,'status','--porcelain']).decode().strip()
        if branch != 'main' or dirty:
            raise DeployError('Deployment requires main and a clean Git working tree')
        head = self.runner.run(['git','-C',ROOT,'rev-parse','HEAD']).decode().strip()
        expected = os.environ.get('EXPECTED_COMMIT', head)
        if expected != head:
            raise DeployError('EXPECTED_COMMIT does not match HEAD')
        remote = self.runner.run(['git','-C',ROOT,'rev-parse','origin/main']).decode().strip()
        if remote != head:
            raise DeployError('HEAD differs from origin/main; finish git pull first')
        self.state['commit'] = head
        log('Git: main, clean, commit '+head)

    def syntax_checks(self):
        # Do not create __pycache__ in a root-owned production checkout.
        for directory in ('app','scripts','services/gab-ocr'):
            for path in (ROOT/directory).glob('*.py'):
                compile(path.read_bytes(), str(path), 'exec')
        for path in (ROOT/'app/static').glob('*.js'):
            self.runner.run(['node','--check',path])
        for path in (ROOT/'scripts').glob('*.sh'):
            self.runner.run(['bash','-n',path])
        self.workflow = validate_workflow(read_workflow(ROOT/'n8n/workflows/GAB02InvImp20261.json'))
        for node in self.workflow['nodes']:
            if 'jsCode' in node.get('parameters',{}):
                self.runner.run(['node','-e', "new (require('vm').Script)('(async function(){'+require('fs').readFileSync(0,'utf8')+'})')"],
                                data=node['parameters']['jsCode'].encode())

    def preflight(self, *, rebuild_ocr=False, bootstrap=False, check=False, component='all'):
        self.rebuild_ocr = rebuild_ocr
        self.revision_checks()
        self.syntax_checks()
        if self.config['database'] != 'gab_crm':
            raise DeployError('Only database gab_crm is supported')
        compose = self.runner.json(self.compose('config','--format','json'))
        service = compose.get('services',{}).get(self.config['ocr_service'])
        if not service:
            raise DeployError('gab_ocr service is missing from Compose')
        build = service.get('build',{})
        if not isinstance(build,dict) or Path(build.get('context','')).resolve() != (ROOT/'services/gab-ocr').resolve() or build.get('dockerfile','Dockerfile') != 'Dockerfile':
            raise DeployError('OCR build context must be '+str(ROOT/'services/gab-ocr')+' and Dockerfile must be Dockerfile')
        ids = self.runner.run(self.compose('ps','-q',self.config['ocr_service'])).decode().split()
        if len(ids) != 1:
            raise DeployError('Exactly one running gab_ocr container is required')
        self.ocr_container = ids[0]
        for key,name in [('crm',self.config['crm_container']),('ocr',self.ocr_container),
                         ('n8n',self.config['n8n_container']),('postgres',self.config['postgres_container'])]:
            self.snapshots[key] = self.inspect(name)
            if not self.snapshots[key]['State']['Running']:
                raise DeployError(key+' container is not running')
        if (self.snapshots['crm']['Config'].get('Labels') or {}).get('com.docker.compose.service'):
            raise DeployError('CRM unexpectedly belongs to Compose; inspect before deployment')
        for key in ('crm','ocr'):
            host=self.snapshots[key]['HostConfig']
            if host.get('AutoRemove') or host.get('ContainerIDFile'):
                raise DeployError('AutoRemove/ContainerIDFile prevents a safe container rollback')
        env = environment(self.snapshots['n8n'])
        if env.get('N8N_USE_WORKFLOW_PUBLICATION_SERVICE','true').lower() not in ('true','1'):
            raise DeployError('n8n publication service is disabled; no global n8n restart is permitted')
        interval = int(env.get('N8N_WORKFLOW_PUBLICATION_OUTBOX_POLL_INTERVAL_MS', '15000'))
        # Config may express milliseconds; never wait less than the documented
        # 15-second default. A longer configured interval is also respected.
        self.publication_wait = max(int(self.config['publication_wait_seconds']), 2*interval/1000+5)
        version = self.n8n('--version').decode().strip().splitlines()[-1]
        if version != self.config['n8n_version']:
            raise DeployError('Expected n8n '+self.config['n8n_version']+', found '+version)
        for command in ('import:workflow','publish:workflow','unpublish:workflow'):
            help_text = self.n8n(command,'--help').decode()
            if ('--input' if command=='import:workflow' else '--id') not in help_text:
                raise DeployError('Required n8n CLI flags are unavailable')
        if self.sql('SELECT current_database()') != 'gab_crm':
            raise DeployError('Database identity check failed')
        self.sql("SELECT 1 FROM gab_crm_import_rows LIMIT 0")
        self.assert_idle()
        self.assert_no_mail_dispatch()
        self.publication_status()
        self.all_workflows = self.export_workflows()
        matches = [w for w in self.all_workflows if w.get('id') == WORKFLOW_ID]
        if len(matches)!=1:
            raise DeployError('Exactly one GAB 02 must already exist')
        self.previous = matches[0]
        if len(self.previous.get('nodes',[])) != 27:
            raise DeployError('Live GAB 02 does not have the audited 27 nodes')
        self.before_other = fingerprint(self.all_workflows)
        if component=='mailings' or (component=='all' and environment(self.snapshots['crm']).get('GAB_MAILING_WEBHOOK_URL')):
            self.verify_mail_sender()
        # Fail before database/container changes if runtime compatibility is bad.
        health_check(self.runner,self.config,self.config['n8n_container'],'n8n',retries=False)
        if component == 'workflow':
            health_check(self.runner,self.config,self.config['crm_container'],'crm',retries=False)
        if not rebuild_ocr:
            health_check(self.runner,self.config,self.ocr_container,'ocr',retries=False)
        paths = [m['Source'] for m in self.snapshots['crm'].get('Mounts',[]) if m['Type']=='bind']
        if not paths:
            raise DeployError('CRM document bind mount is missing')
        for path in paths + [self.config['compose_file']] + self.config['extra_config_paths']:
            if not Path(path).exists():
                raise DeployError('Backup path is missing: '+path)
        self.backup_paths = paths + [self.config['compose_file']]
        parent = Path(self.config['compose_file']).parent
        for path in (parent/'.env', ROOT/'.env', Path(os.environ.get('GAB_DEPLOY_CONFIG','/etc/gab-invest/deploy.json'))):
            if path.exists(): self.backup_paths.append(str(path))
        self.backup_paths += self.config['extra_config_paths']
        # n8n's config contains the local encryption key when it is not supplied
        # as an env var. Docker inspect (also backed up) preserves env-based keys.
        for mount in self.snapshots['n8n'].get('Mounts',[]):
            if mount['Destination']=='/home/node/.n8n':
                path=Path(mount['Source'])/'config'
                if path.exists(): self.backup_paths.append(str(path))
        for key in ('backup_dir','state_dir','age_identity'):
            candidate = Path(self.config[key]).resolve()
            if candidate.is_relative_to(ROOT.resolve()):
                raise DeployError(key+' must be outside Git')
        backup = Path(self.config['backup_dir']).resolve()
        for path in self.backup_paths:
            p=Path(path).resolve()
            if p.is_dir() and backup.is_relative_to(p):
                raise DeployError('Backup directory cannot be inside an archived directory')
        missing = [tool for tool in ('age','age-keygen') if not shutil.which(tool)]
        identity = Path(self.config['age_identity'])
        if check:
            if component!='ocr' and (missing or (not identity.exists() and not self.config['age_recipient'])):
                log('Backup setup needed: run deployment with --bootstrap-backup (installs age and creates a root-only key).')
        else:
            if missing and bootstrap:
                if os.geteuid()!=0 or not shutil.which('apt-get'):
                    raise DeployError('Install age/age-keygen with the OS package manager first')
                log('Installing age from configured OS package repositories…')
                self.runner.run(['apt-get','update'])
                self.runner.run(['apt-get','install','-y','--no-install-recommends','age'])
                missing=[]
            if missing:
                raise DeployError('Missing backup tools: '+', '.join(missing)+'; use --bootstrap-backup')
            if not identity.exists() and not self.config['age_recipient'] and bootstrap:
                identity.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                os.chmod(identity.parent,0o700)
                # age-keygen refuses to overwrite an existing identity.
                self.runner.run(['age-keygen','-o',identity])
                os.chmod(identity,0o600)
                log('Created private backup key at '+str(identity)+'; copy it to separate protected storage.')
            if identity.exists():
                if identity.stat().st_mode & 0o077:
                    raise DeployError('AGE identity must have permissions 0600')
                derived = self.runner.run(['age-keygen','-y',identity]).decode().strip()
                self.recipient = self.config['age_recipient'] or derived
                if self.config['age_recipient'] and self.recipient!=derived:
                    raise DeployError('Configured AGE recipient and recovery identity do not match')
            elif self.config['age_recipient']:
                raise DeployError('Recovery identity is required to verify every backup before changes')
            else:
                raise DeployError('Backup key is not initialized; use --bootstrap-backup')
        if self.engine is None and not check:
            self.engine=Engine(self.runner)
        log('Preflight OK: standalone CRM; OCR Compose context; PostgreSQL; n8n 2.42.7; GAB 02: 27 nodes, no mail nodes.')

    def begin(self):
        release=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+self.state['commit'][:10]+'-'+uuid.uuid4().hex[:6]
        self.state['release']=release
        directory=Path(self.config['state_dir'])/release
        directory.mkdir(parents=True,mode=0o700)
        os.chmod(directory,0o700)
        self.state_path=directory/'deployment.json'
        self.state['config']=self.config
        self.state['workflow_was_active']=bool(self.previous.get('activeVersionId') or self.previous.get('active'))
        self.save()

    def backup(self):
        target=Path(self.config['backup_dir'])
        target.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(target,0o700)
        output=target/(self.state['release']+'.tar.age')
        partial=output.with_suffix('.partial')
        with tempfile.TemporaryDirectory(prefix='gab-backup-',dir=target) as tmp:
            tmp=Path(tmp);os.chmod(tmp,0o700)
            write_json(tmp/'containers.json',self.snapshots)
            write_json(tmp/'workflows.json',self.all_workflows)
            write_json(tmp/'workflow-target.json',[self.previous])
            if self.previous.get('activeVersionId'):
                remote='/tmp/gab-published-'+uuid.uuid4().hex+'.json';self.remote_files.append(remote)
                self.n8n('export:workflow','--id='+WORKFLOW_ID,'--published','--output='+remote)
                raw=self.runner.run(['docker','exec',self.config['n8n_container'],'cat',remote])
                (tmp/'workflow-published.json').write_bytes(raw)
                self.previous_published=json.loads(raw)[0]
                self.runner.run(['docker','exec',self.config['n8n_container'],'rm','-f',remote]);self.remote_files.remove(remote)
            write_json(tmp/'release.json',self.state)
            dump=tmp/'gab_crm.dump'
            with dump.open('wb') as handle:
                self.runner.run(['docker','exec',self.config['postgres_container'],'pg_dump','-U',
                    self.config['postgres_user'],'-d','gab_crm','-Fc'],output=handle)
            with dump.open('rb') as handle:
                # subprocess stdin is streamed, not loaded into RAM.
                p=subprocess.run(['docker','exec','-i',self.config['postgres_container'],'pg_restore','--list'],
                                 stdin=handle,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                if p.returncode: raise DeployError('Database backup validation failed')
            remote='/tmp/gab-credentials-'+uuid.uuid4().hex+'.json';self.remote_files.append(remote)
            self.n8n('export:credentials','--all','--output='+remote)
            (tmp/'credentials-encrypted.json').write_bytes(self.runner.run(['docker','exec',self.config['n8n_container'],'cat',remote]))
            self.runner.run(['docker','exec',self.config['n8n_container'],'rm','-f',remote]);self.remote_files.remove(remote)
            self.runner.run(['docker','image','save','-o',tmp/'previous-images.tar',
                             self.snapshots['crm']['Image'],self.snapshots['ocr']['Image']])
            with tarfile.open(tmp/'settings-documents.tar','w') as archive:
                for index,path in enumerate(dict.fromkeys(self.backup_paths)):
                    archive.add(path,arcname='paths/'+str(index)+'/'+Path(path).name)
            write_json(tmp/'path-map.json',list(dict.fromkeys(self.backup_paths)))
            tar=subprocess.Popen(['tar','-C',str(tmp),'-cf','-','.'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            try:
                with partial.open('xb') as encrypted:
                    os.chmod(partial,0o600)
                    age=subprocess.run(['age','-r',self.recipient],stdin=tar.stdout,stdout=encrypted,stderr=subprocess.PIPE)
                tar.stdout.close()
                tar.wait()
                if age.returncode or tar.returncode: raise DeployError('Backup encryption failed')
                # Authenticate the whole encrypted stream before touching production.
                verified=subprocess.run(['age','-d','-i',self.config['age_identity'],str(partial)],
                    stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                if verified.returncode: raise DeployError('Backup decryption verification failed')
                partial.replace(output)
            finally:
                if tar.poll() is None:tar.kill();tar.wait()
                tar.stdout.close()
                partial.unlink(missing_ok=True)
        digest=hashlib.sha256()
        with output.open('rb') as handle:
            for chunk in iter(lambda:handle.read(1024*1024),b''):digest.update(chunk)
        output.with_suffix(output.suffix+'.sha256').write_text(digest.hexdigest()+'  '+output.name+'\n')
        self.state['backup']=str(output)
        self.save()
        log('Encrypted backup verified: '+str(output))

    def quiesce(self):
        self.state['workflow_touched']=True;self.save()
        self.n8n('unpublish:workflow','--id='+WORKFLOW_ID)
        log('Waiting for GAB 02 to unpublish; other workflows remain running…')
        self.wait_for_publication()
        self.assert_idle()
        self.assert_other_workflows()

    def assert_other_workflows(self):
        if fingerprint(self.export_workflows())!=self.before_other:
            raise DeployError('Another workflow changed concurrently; no changes to it will be made')

    def build_crm(self):
        image='gab-review:main-'+self.state['commit'][:12]
        log('Building CRM from current repository Dockerfile…')
        self.runner.run(['docker','build','--label','gab.commit='+self.state['commit'],
                         '-f',ROOT/'Dockerfile','-t',image,ROOT])
        self.state['new_crm_image']=image;self.save()

    def migrate(self):
        log('Applying idempotent import-editor migration…')
        for migration in ('20261115_import_enrichment.sql','20261119_import_editor.sql','20261119_bulk_mailing.sql','20261119_import_progress.sql','20261119_import_page_cache.sql','20261120_import_fragments.sql'):
            self.sql(None,data=(ROOT/'migrations'/migration).read_bytes())
        found=self.sql("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='gab_crm_import_rows' AND column_name IN ('manual_overrides','edit_version')")
        if found!='2':raise DeployError('Editor migration postcondition failed')
        self.state['migration_applied']=True;self.save()

    def update_crm(self):
        swap=ContainerSwap(self.runner,self.engine,self.state['swaps'],self.save)
        swap.replace(self.snapshots['crm'],self.state['new_crm_image'],self.state['release'])
        health_check(self.runner,self.config,self.config['crm_container'],'crm')
        log('CRM health, protected import API, static assets and editor routes: OK')

    def update_ocr(self):
        if self.rebuild_ocr:
            log('Explicit OCR rebuild: services/gab-ocr/Dockerfile…')
            self.runner.run(self.compose('build',self.config['ocr_service']))
            swap=ContainerSwap(self.runner,self.engine,self.state['swaps'],self.save)
            swap.replace(self.snapshots['ocr'],self.snapshots['ocr']['Config']['Image'],self.state['release'])
        name=self.snapshots['ocr']['Name'].lstrip('/')
        health_check(self.runner,self.config,name,'ocr')
        log('OCR '+self.config['ocr_version']+' health and rus+eng: OK'+(' (rebuilt)' if self.rebuild_ocr else ' (no rebuild)'))

    def import_workflow(self, workflow):
        if workflow.get('id')!=WORKFLOW_ID:
            raise DeployError('Refusing to import another workflow')
        document=copy.deepcopy(workflow)
        document['active']=False;document['activeVersionId']=None
        document.pop('shared',None)
        document.pop('versionMetadata',None)
        document['versionId']=str(uuid.uuid4())
        remote='/tmp/gab-import-'+uuid.uuid4().hex+'.json';self.remote_files.append(remote)
        # stdin avoids transmitting secrets in process arguments or CLI output.
        self.runner.run(['docker','exec','-i',self.config['n8n_container'],'sh','-c',
            'umask 077; cat > "$1"','gab-import',remote],data=json.dumps([document]).encode())
        # root creates the file; restrict readability to n8n's normal node user.
        self.runner.run(['docker','exec',self.config['n8n_container'],'chown','node:node',remote])
        try:self.n8n('import:workflow','--input='+remote)
        finally:
            self.runner.run(['docker','exec',self.config['n8n_container'],'rm','-f',remote]);self.remote_files.remove(remote)

    def publish(self):
        self.import_workflow(self.workflow)
        current=next(w for w in self.export_workflows() if w['id']==WORKFLOW_ID)
        validate_workflow(current)
        for key in ('nodes','connections'):
            if current[key]!=self.workflow[key]:raise DeployError('Imported workflow differs from the verified source')
        state=self.workflow_state()
        self.n8n('publish:workflow','--id='+WORKFLOW_ID,'--versionId='+state['versionId'])
        self.wait_for_publication(state['versionId'])
        published=self.workflow_state()
        if not published.get('activeVersionId') or published['activeVersionId']!=state['versionId']:
            raise DeployError('GAB 02 publication verification failed')
        self.assert_other_workflows()
        health_check(self.runner,self.config,self.config['n8n_container'],'n8n')
        log('Only GAB 02 published: 27 nodes; OCR credential retained; no mail/confirmation nodes.')

    def rollback(self):
        errors=[]
        self.assert_no_mail_dispatch()
        # Stop only GAB 02 before switching back to an older CRM/worker protocol.
        if self.state.get('workflow_touched'):
            try:
                self.n8n('unpublish:workflow','--id='+WORKFLOW_ID)
                self.wait_for_publication()
                self.assert_idle()
            except Exception:
                # Do not switch CRM while an import may still be executing.
                self.state['status']='rollback-needs-operator';self.save()
                raise DeployError('Rollback paused: GAB 02 is not idle. Backup and previous containers are retained.') from None
        swap=ContainerSwap(self.runner,self.engine,self.state['swaps'],self.save)
        for record in reversed(self.state['swaps']):
            try:swap.restore(record,self.state['release'])
            except Exception:errors.append('container '+record['name'])
        if self.state.get('workflow_touched') and not errors:
            try:
                if self.state['workflow_was_active']:
                    # Preserve separate draft and published versions: first put
                    # the former published graph into history, then restore the
                    # old draft, then publish that saved historical version.
                    old_published=getattr(self,'previous_published',None) or self.previous
                    self.import_workflow(old_published)
                    published_version=self.workflow_state()['versionId']
                    self.import_workflow(self.previous)
                    self.n8n('publish:workflow','--id='+WORKFLOW_ID,'--versionId='+published_version)
                    self.wait_for_publication(published_version,old_published)
                    if self.workflow_state().get('activeVersionId')!=published_version:
                        raise DeployError('Previous publication was not restored')
                else:
                    self.import_workflow(self.previous)
                    if self.workflow_state().get('activeVersionId'):
                        raise DeployError('Previous inactive state was not restored')
                restored=next(w for w in self.export_workflows() if w['id']==WORKFLOW_ID)
                if any(restored[k]!=self.previous[k] for k in ('nodes','connections')):
                    raise DeployError('Previous workflow content was not restored')
                self.assert_other_workflows()
            except Exception:errors.append('GAB 02')
        if not errors:
            try:
                # The previous CRM uses the same authenticated /api/health route.
                health_check(self.runner,self.config,self.config['crm_container'],'crm-basic')
                health_check(self.runner,self.config,self.snapshots['ocr']['Name'].lstrip('/'),'ocr-basic')
            except Exception:errors.append('health')
        self.state['status']='rollback-failed' if errors else 'rolled-back';self.save()
        if errors:raise DeployError('Rollback needs operator attention: '+', '.join(errors))
        log('Rollback complete. Previous containers/workflow restored; database was not restored or deleted.')

    def perform(self, stage, operation):
        self.state['stage']=stage;self.save()
        log('[GAB] '+stage)
        return operation()

    def execute(self, component='all'):
        self.begin()
        try:
            self.perform('Резервное копирование',self.backup)
            if component in ('all','crm','mailings'):self.perform('Сборка CRM',self.build_crm)
            if component not in ('backup','mailings'):self.perform('Остановка только GAB 02',self.quiesce)
            if component in ('all','crm','mailings'):
                self.perform('Миграция БД',self.migrate)
                self.perform('Переключение и проверка CRM',self.update_crm)
            if component in ('all','ocr'):self.perform('Проверка OCR',self.update_ocr)
            if component in ('all','workflow'):self.perform('Импорт и публикация GAB 02',self.publish)
            elif component in ('crm','ocr') and self.state['workflow_was_active']:
                # Old active workflow is only safe if it already understands tokens.
                # The component preflight enforces this before any mutation.
                self.n8n('publish:workflow','--id='+WORKFLOW_ID)
                self.wait_for_publication(self.workflow_state()['versionId'],self.previous)
            self.assert_other_workflows()
            self.state['status']='completed';self.save()
            log('SUCCESS: '+component+'; commit '+self.state['commit']+'; journal '+str(self.state_path))
            if component!='backup':log('Rollback: sudo bash '+str(ROOT/'scripts/rollback.sh')+' '+str(self.state_path))
        except BaseException:
            self.state['status']='failed';self.save()
            if self.state['swaps'] or self.state['workflow_touched']:
                try:self.rollback()
                except BaseException as exc:log('ROLLBACK INCOMPLETE: '+str(exc))
            raise
        finally:
            for path in self.remote_files:
                try:self.runner.run(['docker','exec',self.config['n8n_container'],'rm','-f',path])
                except DeployError:pass


def read_recovery(archive, identity):
    """Read only selected regular JSON files; never extract investor documents."""
    process=subprocess.Popen(['age','-d','-i',identity,archive],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    result={}
    try:
        with tarfile.open(fileobj=process.stdout,mode='r|') as tar:
            for member in tar:
                name=member.name.removeprefix('./')
                if name in {'containers.json','workflow-target.json','workflow-published.json','workflows.json'}:
                    if not member.isfile() or member.size > 100*1024*1024:
                        raise DeployError('Invalid backup metadata')
                    with tar.extractfile(member) as handle:
                        result[name]=json.load(handle)
        process.stdout.close();process.wait()
        if process.returncode:raise DeployError('Cannot decrypt recovery archive')
    except (tarfile.TarError, json.JSONDecodeError, OSError):
        raise DeployError('Cannot authenticate/decrypt recovery archive') from None
    finally:
        if process.poll() is None:process.kill();process.wait()
        process.stdout.close()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component',choices=['all','crm','ocr','workflow','mailings','backup','rollback'])
    parser.add_argument('journal',nargs='?')
    parser.add_argument('--check',action='store_true',help='Read-only preflight; never deploy')
    parser.add_argument('--bootstrap-backup',action='store_true',help='Install OS age package if missing; create private recovery key if absent')
    parser.add_argument('--rebuild-ocr',action='store_true',help='Explicitly rebuild OCR from its audited Compose context')
    args=parser.parse_args()
    if args.rebuild_ocr and args.component not in ('all','ocr'):
        parser.error('--rebuild-ocr is only valid for all or ocr')
    os.umask(0o077)
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,terminate)
    deployment=Deployment()
    # A single lock covers all entry points and rollback, including backup.
    lock_path=Path('/run/lock/gab-invest-deploy.lock')
    if os.geteuid()!=0:
        raise DeployError('Run as root: sudo bash scripts/deploy-all.sh (read-only audit also needs Docker/root access)')
    with lock_path.open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise DeployError('Another GAB deployment/backup is running') from None
        if args.component=='rollback':
            if not args.journal:raise DeployError('Pass deployment.json from the failed deployment')
            deployment.state_path=Path(args.journal)
            deployment.state=json.loads(deployment.state_path.read_text())
            deployment.config=deployment.state['config']
            archive=deployment.state.get('backup')
            if not archive:raise DeployError('Journal has no verified backup')
            data=read_recovery(archive,deployment.config['age_identity'])
            deployment.snapshots=data['containers.json']
            deployment.previous=data['workflow-target.json'][0]
            deployment.previous_published=(data.get('workflow-published.json') or [deployment.previous])[0]
            deployment.before_other=fingerprint(data['workflows.json'])
            deployment.publication_wait=deployment.config['publication_wait_seconds']
            deployment.engine=Engine(deployment.runner)
            deployment.rollback()
            return
        deployment.preflight(rebuild_ocr=args.rebuild_ocr,bootstrap=args.bootstrap_backup,
                             check=args.check or (args.component=='ocr' and not args.rebuild_ocr),component=args.component)
        if args.component=='ocr' and not args.rebuild_ocr:
            log('OCR is healthy: no rebuild, restart, backup setup or workflow change requested.')
            return
        if args.check:
            log('CHECK ONLY: no migration, build, restart, import, publish or key creation executed.')
            return
        if args.component in ('crm','ocr'):
            # Avoid reactivating the old 27-node workflow against a CRM requiring
            # worker tokens. Use deploy-all to migrate both sides together.
            if deployment.previous.get('active'):
                try:validate_workflow(deployment.previous)
                except ValueError:raise DeployError('Use deploy-all: active workflow is incompatible with the new worker-token protocol') from None
        deployment.execute(args.component)


if __name__=='__main__':
    try:main()
    except (DeployError,ValueError,KeyError) as exc:
        log('ERROR: '+str(exc));sys.exit(1)
    except KeyboardInterrupt:
        log('Interrupted; consult the deployment journal and rollback status.');sys.exit(130)

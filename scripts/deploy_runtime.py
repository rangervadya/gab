"""Deployment primitives. Commands never print container environments/secrets."""
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import quote


class DeployError(RuntimeError):
    pass


class Runner:
    def run(self, args, *, data=None, output=None):
        try:
            p = subprocess.run([str(a) for a in args], input=data,
                stdout=output if output is not None else subprocess.PIPE,
                stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            raise DeployError(f'Cannot execute {args[0]}: {exc.__class__.__name__}') from None
        if p.returncode:
            # stderr may include passwords, personal records or complete Docker
            # create payloads. Show only the operation; keep secrets out of logs.
            raise DeployError(f'Command failed: {args[0]} {args[1] if len(args)>1 else ""} (exit {p.returncode})')
        return p.stdout if output is None else b''

    def json(self, args):
        return json.loads(self.run(args))


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=90)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Engine:
    def __init__(self, runner):
        host = runner.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).decode().strip()
        if not host.startswith('unix://'):
            raise DeployError('Only a local Docker Unix socket is supported')
        self.socket_path = host.removeprefix('unix://')
        self.version = self.request('GET', '/version', versioned=False)['ApiVersion']

    def request(self, method, path, body=None, *, versioned=True):
        connection = UnixHTTPConnection(self.socket_path)
        data = json.dumps(body).encode() if body is not None else None
        try:
            connection.request(method, ('/v' + self.version if versioned else '') + path,
                               body=data, headers={'Content-Type':'application/json'})
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 300:
                raise DeployError(f'Docker Engine {method} operation failed (HTTP {response.status})')
            return json.loads(raw) if raw else None
        finally:
            connection.close()


def environment(inspect):
    return dict(item.split('=', 1) for item in inspect['Config'].get('Env', []) if '=' in item)


def endpoint_config(info):
    return {key: copy.deepcopy(info[key]) for key in
            ('IPAMConfig', 'Links', 'Aliases', 'DriverOpts', 'GwPriority')
            if info.get(key) is not None}


def clone_payload(inspect, image):
    """Docker inspect Config is the container-create Config, with HostConfig.

    Keep its exact env, command, entrypoint, user, limits, bind/volume mounts,
    restart policy, security flags and port bindings. Endpoint IDs and dynamically
    allocated IP addresses belong to the old container and must not be replayed.
    """
    payload = copy.deepcopy(inspect['Config'])
    payload['Image'] = image
    payload['HostConfig'] = copy.deepcopy(inspect['HostConfig'])
    payload['NetworkingConfig'] = {'EndpointsConfig': {
        name: endpoint_config(info) for name, info in inspect['NetworkSettings']['Networks'].items()}}
    # Anonymous/named volumes must reuse the actual existing volume, rather than
    # silently creating a fresh anonymous volume from Config.Volumes.
    mounts = payload['HostConfig'].setdefault('Mounts', [])
    occupied = {m.get('Target') for m in mounts}
    occupied |= {b.split(':')[1] for b in payload['HostConfig'].get('Binds') or [] if ':' in b}
    for mount in inspect.get('Mounts', []):
        if mount['Type'] == 'volume' and mount['Destination'] not in occupied:
            mounts.append({'Type':'volume', 'Source':mount['Name'],
                           'Target':mount['Destination'], 'ReadOnly':not mount['RW']})
    return payload


def assert_runtime_preserved(before, after):
    for key in ('Cmd','Entrypoint','User','WorkingDir','Hostname','Healthcheck'):
        if before['Config'].get(key)!=after['Config'].get(key):
            raise DeployError('Container configuration changed unexpectedly: '+key)
    new_environment=environment(after)
    if any(new_environment.get(key)!=value for key,value in environment(before).items()):
        raise DeployError('Container environment was not preserved')
    for key in ('Binds','PortBindings','NetworkMode','RestartPolicy','Memory','MemorySwap','NanoCpus',
                'CpuShares','CpusetCpus','ReadonlyRootfs','CapAdd','CapDrop','SecurityOpt','LogConfig',
                'Privileged','Devices','Dns','DnsSearch','ExtraHosts','Ulimits','ShmSize','PidsLimit'):
        if before['HostConfig'].get(key)!=after['HostConfig'].get(key):
            raise DeployError('Container HostConfig changed unexpectedly: '+key)
    def mount_signature(data):
        return sorted((m['Type'],m.get('Name') if m['Type']=='volume' else m['Source'],
                       m['Destination'],m['RW']) for m in data.get('Mounts',[]))
    if mount_signature(before)!=mount_signature(after):
        raise DeployError('Container mounts were not preserved')
    if set(before['NetworkSettings']['Networks'])!=set(after['NetworkSettings']['Networks']):
        raise DeployError('Container networks were not preserved')
    for name,info in before['NetworkSettings']['Networks'].items():
        if endpoint_config(info)!=endpoint_config(after['NetworkSettings']['Networks'][name]):
            raise DeployError('Configured network aliases/static addresses were not preserved')


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.partial')
    with temporary.open('w') as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class ContainerSwap:
    """Keep the old container stopped for rollback; never remove its volumes."""
    def __init__(self, runner, engine, journal, save):
        self.runner, self.engine, self.journal, self.save = runner, engine, journal, save

    def replace(self, inspect, image, release):
        name = inspect['Name'].lstrip('/')
        old_id = inspect['Id']
        record = {'name': name, 'old_id': old_id, 'old_image': inspect['Image'],
                  'old_running': inspect['State']['Running'],
                  'networks':{n:endpoint_config(v) for n,v in inspect['NetworkSettings']['Networks'].items()},
                  'new_id':None, 'detached':[]}
        self.journal.append(record)
        self.save()
        self.runner.run(['docker','stop','--time','30',old_id])
        self.runner.run(['docker','rename',old_id,name+'-rollback-'+release])
        for network in record['networks']:
            self.engine.request('POST','/networks/'+quote(network,safe='')+'/disconnect',
                                {'Container':old_id,'Force':True})
            record['detached'].append(network)
            self.save()
        # A unique deployment label lets rollback discover a container even if
        # the process dies between Engine create and writing its returned ID.
        payload = clone_payload(inspect, image)
        payload['Labels'] = dict(payload.get('Labels') or {})
        payload['Labels']['gab.deploy.release'] = release
        created = self.engine.request('POST','/containers/create?name='+quote(name,safe=''),payload)
        record['new_id'] = created['Id']
        self.save()
        self.runner.run(['docker','start',record['new_id']])
        assert_runtime_preserved(inspect,self.runner.json(['docker','inspect',record['new_id']])[0])
        return record['new_id']

    def restore(self, record, release):
        name, old_id = record['name'], record['old_id']
        new_id = record.get('new_id')
        if not new_id:
            candidates = self.runner.json(['docker','inspect',name]) if self._exists(name) else []
            if candidates and (candidates[0]['Config'].get('Labels') or {}).get('gab.deploy.release') == release:
                new_id = candidates[0]['Id']
        if new_id and self._exists(new_id):
            # Remove only the new container created by this deployment, no -v.
            data = self.runner.json(['docker','inspect',new_id])[0]
            if (data['Config'].get('Labels') or {}).get('gab.deploy.release') != release:
                raise DeployError('Rollback refused to remove an unrelated container')
            self.runner.run(['docker','rm','--force',new_id])
        if not self._exists(old_id):
            raise DeployError('Previous container is missing; restore it from the encrypted backup')
        current = self.runner.json(['docker','inspect',old_id])[0]
        if current['Name'].lstrip('/') != name:
            self.runner.run(['docker','rename',old_id,name])
        for network, endpoint in record['networks'].items():
            if network not in current['NetworkSettings']['Networks']:
                self.engine.request('POST','/networks/'+quote(network,safe='')+'/connect',
                                    {'Container':old_id,'EndpointConfig':endpoint})
        if record['old_running']:
            self.runner.run(['docker','start',old_id])

    def _exists(self, name):
        try:
            self.runner.run(['docker','inspect',name])
            return True
        except DeployError:
            return False


HEALTH_PROGRAM = r'''
import base64,json,os,urllib.request
kind=os.environ['GAB_HEALTH_KIND']
paths=['/api/health'] if kind in ('crm','crm-basic') else ['/health']
if kind=='crm': paths += ['/api/mailings/imports?limit=1','/static/crm.css','/static/crm.js','/static/imports.js','/openapi.json']
if kind=='ocr': paths += ['/openapi.json']
if kind=='n8n': paths=['/healthz/readiness']
port='5678' if kind=='n8n' else '8000'
results={}
for path in paths:
 req=urllib.request.Request('http://127.0.0.1:'+port+path)
 if kind in ('crm','crm-basic'):
  pair=os.environ['GAB_APP_USER']+':'+os.environ['GAB_APP_PASSWORD']
  req.add_header('Authorization','Basic '+base64.b64encode(pair.encode()).decode())
 with urllib.request.urlopen(req,timeout=10) as response:
  data=response.read()
  if path in ['/api/health','/health','/openapi.json']: results[path]=json.loads(data)
  elif path.startswith('/static/') and not data: raise RuntimeError('Empty static asset')
if kind in ('crm','crm-basic'):
 assert results['/api/health']['status']=='ok'
if kind=='crm':
 paths=results['/openapi.json']['paths']
 assert 'patch' in paths['/api/mailings/imports/{batch_id}/rows/{row_id}']
 assert 'get' in paths['/api/mailings/imports/{batch_id}/rows/{row_id}/match-preview']
 assert 'post' in paths['/api/mailings/imports/{batch_id}/rows/{row_id}/confirm']
if kind=='ocr':
 assert results['/health']['version']==os.environ['GAB_EXPECTED_OCR_VERSION']
 assert {'rus','eng'} <= set(results['/health']['languages'])
 assert {'/extract/text','/extract/word-text'} <= set(results['/openapi.json']['paths'])
print('ok')
'''


def health_check(runner, config, container, kind, *, retries=True):
    attempts = int(config['health_attempts']) if retries else 1
    for attempt in range(attempts):
        try:
            if kind == 'n8n':
                runner.run(['docker','exec',container,'node','-e',
                    "fetch('http://127.0.0.1:'+(process.env.N8N_PORT||'5678')+'/healthz/readiness').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"])
            else:
                runner.run(['docker','exec','-e','GAB_HEALTH_KIND='+kind,
                    '-e','GAB_EXPECTED_OCR_VERSION='+config['ocr_version'], container,'python','-c',HEALTH_PROGRAM])
            return
        except DeployError:
            if attempt + 1 == attempts:
                raise DeployError(f'{kind} health-check failed') from None
            time.sleep(3)

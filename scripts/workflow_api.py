"""Updates only GAB 02 via the public API; never activates or executes it."""
import json
import os
import urllib.request
from pathlib import Path

workflow_id = 'GAB02InvImp20261'
base = os.environ['N8N_URL'].rstrip('/')
if not base.startswith('https://'):
    raise SystemExit('N8N_URL must use HTTPS')
url = base + '/api/v1/workflows/' + workflow_id
headers = {'X-N8N-API-KEY':os.environ['N8N_API_KEY'], 'Content-Type':'application/json'}

def call(method, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)

current = call('GET')
if current.get('id') != workflow_id or current.get('active'):
    raise SystemExit('Stop GAB 02 and wait for its executions to finish before replacing it')
source = json.loads((Path(__file__).resolve().parents[1] / 'n8n/workflows/GAB02InvImp20261.json').read_text())[0]
if source['id'] != workflow_id:
    raise SystemExit('Unexpected workflow ID')
body = {k:source[k] for k in ('name','nodes','connections','settings') if k in source}
call('PUT', body)
saved = call('GET')
if saved.get('active') or saved['nodes'] != body['nodes'] or saved['connections'] != body['connections']:
    raise SystemExit('Workflow verification failed')
print('GAB 02 updated and remains inactive. Activation and synthetic run require an operator.')

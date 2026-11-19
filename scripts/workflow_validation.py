"""Pure validation of the single approved production workflow."""
import json
from pathlib import Path

WORKFLOW_ID = 'GAB02InvImp20261'
OCR_CREDENTIAL = {'id': '6Wb6WQE64aBvpr95', 'name': 'GAB OCR API'}
EXPECTED_NAMES = {
    'Manual start', 'Every minute', 'List uploaded batches', 'Select oldest uploaded batch',
    'Claim batch', 'Claim and normalize', 'Is PDF?', 'Download PDF', 'OCR PDF',
    'Prepare PDF pages', 'Ollama extract investors', 'Parse PDF records', 'Save PDF records',
    'Is XLSX?', 'Download XLSX', 'Extract XLSX', 'Is XLS?', 'Download XLS', 'Extract XLS',
    'Normalize spreadsheet rows', 'Bundle spreadsheet records', 'Save spreadsheet records',
    'Unsupported file format', 'Mark batch failed', 'Is Word?', 'Download Word', 'OCR Word',
}
ALLOWED_TYPES = {'n8n-nodes-base.' + name for name in (
    'manualTrigger', 'scheduleTrigger', 'httpRequest', 'code', 'if', 'extractFromFile')}
EDGES = [
    ('Claim and normalize', 0, 'Is PDF?'), ('Is PDF?', 0, 'Download PDF'),
    ('Is PDF?', 1, 'Is XLSX?'), ('Download PDF', 0, 'OCR PDF'),
    ('OCR PDF', 0, 'Prepare PDF pages'), ('Prepare PDF pages', 0, 'Ollama extract investors'),
    ('Ollama extract investors', 0, 'Parse PDF records'), ('Parse PDF records', 0, 'Save PDF records'),
    ('Is XLSX?', 0, 'Download XLSX'), ('Is XLSX?', 1, 'Is XLS?'),
    ('Download XLSX', 0, 'Extract XLSX'), ('Extract XLSX', 0, 'Normalize spreadsheet rows'),
    ('Is XLS?', 0, 'Download XLS'), ('Is XLS?', 1, 'Is Word?'),
    ('Download XLS', 0, 'Extract XLS'), ('Extract XLS', 0, 'Normalize spreadsheet rows'),
    ('Normalize spreadsheet rows', 0, 'Bundle spreadsheet records'),
    ('Bundle spreadsheet records', 0, 'Save spreadsheet records'),
    ('Is Word?', 0, 'Download Word'), ('Is Word?', 1, 'Unsupported file format'),
    ('Download Word', 0, 'OCR Word'), ('OCR Word', 0, 'Prepare PDF pages'),
]


HTTP_ROUTES = {
    'List uploaded batches': 'http://gab-review:8000/api/mailings/imports?status=uploaded&limit=20',
    'OCR PDF': 'http://gab_ocr:8000/extract/text',
    'OCR Word': 'http://gab_ocr:8000/extract/word-text',
    'Ollama extract investors': 'http://gab_ocr:8000/ollama/chat',
}
for name, suffix in [('Claim batch','claim'),('Download PDF','file'),('Download XLSX','file'),
                     ('Download XLS','file'),('Download Word','file')]:
    HTTP_ROUTES[name] = "={{ 'http://gab-review:8000/api/mailings/imports/' + $json.batch_id + '/" + suffix + "' }}"
for name, suffix in [('Save PDF records','rows'),('Save spreadsheet records','rows'),('Mark batch failed','fail')]:
    HTTP_ROUTES[name] = "={{ 'http://gab-review:8000/api/mailings/imports/' + $('Claim and normalize').first().json.batch_id + '/" + suffix + "' }}"


def read_workflow(path):
    document = json.loads(Path(path).read_text())
    if isinstance(document, list):
        if len(document) != 1:
            raise ValueError('Export must contain exactly one workflow')
        document = document[0]
    if not isinstance(document, dict):
        raise ValueError('Invalid workflow export')
    return document


def validate_workflow(w):
    if w.get('id') != WORKFLOW_ID:
        raise ValueError('Only GAB02InvImp20261 may be deployed')
    nodes = w.get('nodes', [])
    by_name = {n['name']: n for n in nodes}
    if len(nodes) != 27 or set(by_name) != EXPECTED_NAMES or len({n['id'] for n in nodes}) != 27:
        raise ValueError('GAB 02 must have the 27 audited nodes with unique IDs')
    for node in nodes:
        if node['type'] not in ALLOWED_TYPES:
            raise ValueError('Unexpected node type; mail and external integrations are forbidden')
        p = node.get('parameters', {})
        if node['type'] == 'n8n-nodes-base.httpRequest':
            url = p.get('url', '')
            if any(x in url.lower() for x in ('/confirm', '/send', 'smtp', '/mailings/trigger')):
                raise ValueError('Sending mail and confirming imports are forbidden')
            if HTTP_ROUTES.get(node['name']) != url:
                raise ValueError('Unexpected HTTP destination or expression')
        if 'httpHeaderAuth' in node.get('credentials', {}):
            if node['credentials']['httpHeaderAuth'] != OCR_CREDENTIAL:
                raise ValueError('GAB OCR API credential reference changed')
    for name in ('OCR PDF', 'OCR Word'):
        if by_name[name].get('credentials', {}).get('httpHeaderAuth') != OCR_CREDENTIAL:
            raise ValueError('OCR credential is missing')
    connections = w.get('connections', {})
    for source, outputs in connections.items():
        if source not in by_name:
            raise ValueError('Unknown connection source')
        for branches in outputs.values():
            for branch in branches:
                for link in branch:
                    if link.get('node') not in by_name:
                        raise ValueError('Unknown connection target')
    for source, branch, target in EDGES:
        try:
            found = any(link['node'] == target for link in connections[source]['main'][branch])
        except (KeyError, IndexError, TypeError):
            found = False
        if not found:
            raise ValueError(f'Missing branch: {source} -> {target}')
    for name in ('Save PDF records', 'Save spreadsheet records', 'Mark batch failed'):
        if 'worker_token' not in by_name[name]['parameters'].get('jsonBody', ''):
            raise ValueError('Worker token is required on every ingestion/error request')
    if 'worker_token' not in by_name['Claim and normalize']['parameters'].get('jsCode', ''):
        raise ValueError('Claim token must be carried through the workflow')
    pagination=by_name['Ollama extract investors']['parameters'].get('options',{}).get('pagination',{}).get('pagination',{})
    if pagination.get('paginationMode')!='updateAParameterInEachRequest' or pagination.get('maxRequests')!=200 or pagination.get('limitPagesFetched') is not True:
        raise ValueError('Sequential bounded Ollama pagination is required')
    if pagination.get('completeExpression')!='={{ $response.body._gab_last_page === true }}':
        raise ValueError('Ollama must stop after the last completed page')
    return w

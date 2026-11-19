#!/usr/bin/env python3
"""Read-only sender audit. Never calls a webhook or runs a workflow."""
import json
import os
import subprocess
import uuid
from urllib.parse import urlsplit


def run(args):
    p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if p.returncode:
        raise SystemExit('Audit command failed: '+args[0]+' '+args[1])
    return p.stdout


def main():
    crm=json.loads(run(['docker','inspect','gab-review']))[0]
    env=dict(item.split('=',1) for item in crm['Config'].get('Env',[]) if '=' in item)
    url=env.get('GAB_MAILING_WEBHOOK_URL','')
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname!='n8n.techsmysl.ru' or not parts.path.startswith('/webhook/'):
        raise SystemExit('Configured mailing webhook is missing or unexpected; no calls were made')
    route=parts.path.removeprefix('/webhook/').strip('/')
    remote='/tmp/gab-mail-audit-'+uuid.uuid4().hex+'.json'
    container='ai-stack-n8n_ai-1'
    try:
        run(['docker','exec','--user','node',container,'n8n','export:workflow','--all','--output='+remote])
        workflows=json.loads(run(['docker','exec',container,'cat',remote]))
    finally:
        run(['docker','exec',container,'rm','-f',remote])
    matches=[]
    for workflow in workflows:
        hooks=[n for n in workflow.get('nodes',[]) if n.get('type')=='n8n-nodes-base.webhook'
               and str(n.get('parameters',{}).get('path','')).strip('/')==route]
        if hooks:
            # Deliberately omit pinned execution data, static state, shared-user
            # metadata and credential secrets. Credential references are needed.
            matches.append({k:workflow.get(k) for k in ('id','name','active','activeVersionId','nodes','connections','settings')})
    report={'webhook_configured':True,'token_configured':bool(env.get('GAB_MAILING_WEBHOOK_TOKEN')),
            'matching_workflows':matches,'emails_sent':0}
    text=json.dumps(report,ensure_ascii=False,indent=2)
    for key,value in env.items():
        if any(word in key.upper() for word in ('TOKEN','PASSWORD','SECRET','API_KEY')) and len(value)>=6:
            text=text.replace(value,'<REDACTED_SECRET>')
    text=text.replace(route,'<CONFIGURED_MAIL_WEBHOOK_PATH>')
    print(text)


if __name__=='__main__':main()

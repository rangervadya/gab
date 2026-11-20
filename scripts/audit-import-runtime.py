#!/usr/bin/env python3
"""Read-only import/Ollama diagnostics. No model generation, credentials or document text."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess


class AuditError(RuntimeError):
    pass


def run(args,*,include_stderr=False):
    try:
        result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25)
    except (OSError,subprocess.TimeoutExpired):
        raise AuditError('Diagnostic command unavailable or timed out') from None
    if result.returncode:
        # Docker/psql stderr can include configuration; do not echo it.
        raise AuditError('Diagnostic command failed')
    return (result.stdout+(result.stderr if include_stderr else b'')).decode(errors='replace')


def sql(query,database='gab_crm'):
    # All SQL, including discovery, runs in a read-only transaction. IDs are
    # parsed as positive integers before interpolation. Never select payloads.
    text="BEGIN READ ONLY; SET LOCAL statement_timeout='10s'; "+query+'; COMMIT;'
    raw=run(['docker','exec','ai-stack-postgres_ai-1','psql','-X','-qAt',
             '-v','ON_ERROR_STOP=1','-U','n8n_user','-d',database,'-c',text])
    return json.loads(raw.strip())


def service_container(service):
    names=run(['docker','ps','--filter','label=com.docker.compose.service='+service,
               '--format','{{.Names}}']).splitlines()
    if len(names)!=1:raise AuditError('Expected exactly one running '+service+' service')
    return names[0]


def inspect_safe(name):
    doc=json.loads(run(['docker','inspect',name]))[0]
    config=doc.get('Config') or {};host=doc.get('HostConfig') or {};state=doc.get('State') or {}
    allowed={'OLLAMA_NUM_PARALLEL','OLLAMA_MAX_LOADED_MODELS','OLLAMA_MAX_QUEUE',
             'OLLAMA_CONTEXT_LENGTH','OLLAMA_FLASH_ATTENTION','OLLAMA_KV_CACHE_TYPE',
             'OLLAMA_KEEP_ALIVE','OLLAMA_GPU_OVERHEAD'}
    env={key:value for item in config.get('Env') or [] if '=' in item
         for key,value in [item.split('=',1)] if key in allowed}
    # A whitelist, never all environment variables, command arguments or mounts.
    return {'name':name,'image':config.get('Image'),'status':state.get('Status'),
            'oom_killed':state.get('OOMKilled'),'restart_count':doc.get('RestartCount'),
            'memory_limit_bytes':host.get('Memory'),'nano_cpus':host.get('NanoCpus'),
            'gpu_requests':[{k:r.get(k) for k in ('Driver','Count','Capabilities')}
                            for r in host.get('DeviceRequests') or []],
            'ollama_settings':env}


RUNTIME_PROBE=r'''
import json,urllib.request
out={}
for key,url in [('ocr_health','http://127.0.0.1:8000/health'),
                ('ollama_version','http://ollama_ai:11434/api/version'),
                ('ollama_models','http://ollama_ai:11434/api/ps')]:
    try:
        with urllib.request.urlopen(url,timeout=5) as response:
            value=json.loads(response.read(100000))
        if key=='ocr_health':value={k:value.get(k) for k in ('status','version')}
        elif key=='ollama_version':value={'version':value.get('version')}
        else:value=[{k:model.get(k) for k in ('name','size','size_vram','context_length','expires_at')}
                    for model in value.get('models',[])]
        out[key]=value
    except Exception:out[key]={'unavailable':True}
print(json.dumps(out))
'''


def log_summary(raw):
    markers={'out_of_memory':r'out of memory|\boom\b|cuda.*alloc.*fail',
             'queue_pressure':r'queue.*full|server.*busy|too many requests',
             'model_load':r'loading model|starting llama server|load.*runner',
             'timeout':r'timeout|timed out|context deadline exceeded'}
    summary={'markers_in_last_400_lines':{key:len(re.findall(pattern,raw,re.I)) for key,pattern in markers.items()},
             'recent_generation_http':[]}
    # Only request timestamp/status/duration: never IPs, prompt or log messages.
    pattern=r'\[GIN\]\s+(\d{4}/\d{2}/\d{2}\s+-\s+\d{2}:\d{2}:\d{2})\s*\|\s*(\d{3})\s*\|\s*([0-9.a-zµμ ]+)\|[^\n]*?POST\s+"/api/(chat|generate)"'
    for stamp,status,duration,endpoint in re.findall(pattern,raw):
        summary['recent_generation_http'].append({'time':stamp,'status':int(status),
            'duration':duration.strip(),'endpoint':'/api/'+endpoint})
    summary['recent_generation_http']=summary['recent_generation_http'][-12:]
    return summary


def collect(batch_id,execution_id):
    report={'batch_id':batch_id,'execution_id':execution_id,'read_only':True,
            'generation_requests_sent':0,'emails_sent':0}
    def section(name,fn):
        try:report[name]=fn()
        except (AuditError,ValueError,KeyError,TypeError):report[name]={'unavailable':True}
    section('batch',lambda:sql("SELECT row_to_json(b) FROM (SELECT id,status,processing_started_at,processing_finished_at,progress_stage,progress_total,progress_pages,progress_updated_at,n8n_execution_id FROM gab_crm_import_batches WHERE id="+str(batch_id)+') b'))
    section('active_import_batches',lambda:sql("SELECT COALESCE(json_agg(b),'[]'::json) FROM (SELECT id,progress_stage,n8n_execution_id,processing_started_at FROM gab_crm_import_batches WHERE status='processing' ORDER BY id LIMIT 20) b"))
    section('saved_pages',lambda:sql("SELECT COALESCE(json_agg(p ORDER BY p.source_page),'[]'::json) FROM (SELECT source_page,created_at,left(request_hash,12) AS request_hash_prefix,result_json->>'model' AS model,result_json->>'done_reason' AS done_reason,result_json->>'eval_count' AS output_tokens,result_json->>'prompt_eval_count' AS input_tokens,result_json->>'total_duration' AS total_duration_ns,result_json->>'load_duration' AS load_duration_ns,result_json->>'eval_duration' AS eval_duration_ns,length(result_json->'message'->>'content') AS output_characters FROM gab_crm_import_page_results WHERE batch_id="+str(batch_id)+') p'))
    def execution():
        database=run(['docker','exec','ai-stack-n8n_ai-1','printenv','DB_POSTGRESDB_DATABASE']).strip()
        if not database or database=='gab_crm':raise AuditError('Unexpected n8n database')
        selected=sql("SELECT row_to_json(e) FROM (SELECT id,status,\"startedAt\",\"stoppedAt\" FROM execution_entity WHERE \"workflowId\"='GAB02InvImp20261' AND id="+str(execution_id)+') e',database)
        active=sql("SELECT COALESCE(json_agg(e),'[]'::json) FROM (SELECT id,status,\"startedAt\" FROM execution_entity WHERE \"workflowId\"='GAB02InvImp20261' AND status IN ('new','running','waiting') ORDER BY id DESC LIMIT 20) e",database)
        return {'selected':selected,'active':active}
    section('execution',execution)
    def runtime():
        ocr=service_container('gab_ocr')
        return json.loads(run(['docker','exec',ocr,'python','-c',RUNTIME_PROBE]))
    section('runtime',runtime)
    def ollama():
        name=service_container('ollama_ai')
        info=inspect_safe(name)
        stats=json.loads(run(['docker','stats','--no-stream','--format','{{json .}}',name]))
        info['stats']={key:stats.get(key) for key in ('CPUPerc','MemUsage','MemPerc','PIDs')}
        try:info['logs']=log_summary(run(['docker','logs','--tail','400',name],include_stderr=True))
        except AuditError:info['logs']={'unavailable':True}
        return info
    section('ollama_container',ollama)
    def memory():
        wanted={'MemTotal','MemAvailable','SwapTotal','SwapFree'}
        return {key:int(rest.split()[0]) for line in Path('/proc/meminfo').read_text().splitlines()
                if ':' in line for key,rest in [line.split(':',1)] if key in wanted}
    section('host_memory_kib',memory)
    if shutil.which('nvidia-smi'):
        section('gpu',lambda:run(['nvidia-smi','--query-gpu=name,memory.total,memory.used,utilization.gpu',
                                 '--format=csv,noheader,nounits']).strip())
    return report


def positive(value):
    try:number=int(value)
    except ValueError:raise argparse.ArgumentTypeError('Expected positive integer') from None
    if not 1<=number<=9223372036854775807:raise argparse.ArgumentTypeError('Expected positive integer')
    return number


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-id',type=positive,default=8)
    parser.add_argument('--execution-id',type=positive,default=469)
    args=parser.parse_args()
    print(json.dumps(collect(args.batch_id,args.execution_id),ensure_ascii=False,indent=2))


if __name__=='__main__':main()

"""Read-only diagnostics, including privacy boundaries. No Docker/server calls."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('import_runtime_audit',Path(__file__).resolve().parents[1]/'scripts/audit-import-runtime.py')
audit=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class ImportRuntimeAuditTests(unittest.TestCase):
    def test_log_summary_keeps_timings_without_prompt_addresses_or_tokens(self):
        logs='''2026/11/20 loading model PRIVATE PROMPT person@example.test SECRET_TOKEN
[GIN] 2026/11/20 - 08:12:15 | 200 | 8m1.23s | 192.168.1.22 | POST "/api/chat"
2026/11/20 out of memory PRIVATE COMPANY SECRET_TOKEN
[GIN] 2026/11/20 - 08:32:15 | 500 | 20m1.5s | 192.168.1.22 | POST "/api/chat"
'''
        result=audit.log_summary(logs)
        self.assertEqual(result['markers_in_last_400_lines']['out_of_memory'],1)
        self.assertEqual([r['duration'] for r in result['recent_generation_http']],['8m1.23s','20m1.5s'])
        for private in ['SECRET_TOKEN','PRIVATE','example.test','192.168']:
            self.assertNotIn(private,json.dumps(result))

    def test_sql_is_read_only_and_does_not_echo_psql_errors(self):
        with patch.object(audit,'run',return_value='[{"source_page":1}]') as call:
            self.assertEqual(audit.sql('SELECT source_page FROM gab_crm_import_page_results'),[{'source_page':1}])
            query=call.call_args.args[0][-1]
            self.assertTrue(query.startswith("BEGIN READ ONLY; SET LOCAL statement_timeout='10s';"))
        with patch.object(audit.subprocess,'run',return_value=subprocess.CompletedProcess([],1,b'',b'SECRET DATABASE_URL')):
            with self.assertRaises(audit.AuditError) as error:audit.run(['docker','inspect','synthetic'])
            self.assertNotIn('SECRET',str(error.exception))

    def test_container_inspection_omits_secrets_and_commands(self):
        doc=[{'Config':{'Image':'ollama/ollama:synthetic','Env':['PASSWORD=SECRET','OLLAMA_NUM_PARALLEL=1'],
                        'Cmd':['SECRET_COMMAND']},'HostConfig':{'Binds':['/PRIVATE/file:/data'],
                        'DeviceRequests':[{'Driver':'nvidia','Count':1,'Capabilities':[['gpu']],'DeviceIDs':['PRIVATE_ID']}]},
                        'State':{'Status':'running'}}]
        with patch.object(audit,'run',return_value=json.dumps(doc)):
            result=audit.inspect_safe('synthetic')
        self.assertEqual(result['ollama_settings'],{'OLLAMA_NUM_PARALLEL':'1'})
        self.assertNotIn('SECRET',json.dumps(result));self.assertNotIn('PRIVATE',json.dumps(result))

    def test_runtime_probe_uses_only_read_only_endpoints(self):
        calls=[]
        class Response:
            def __init__(self,url):self.url=url
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,limit):
                return json.dumps({'status':'ok','version':'synthetic','SECRET':'private',
                    'models':[{'name':'synthetic','size_vram':0,'SECRET':'private'}]}).encode()
        def request(url,**kwargs):calls.append(url);return Response(url)
        with patch('urllib.request.urlopen',side_effect=request),patch('builtins.print') as output:
            exec(audit.RUNTIME_PROBE,{})
        self.assertEqual(calls,['http://127.0.0.1:8000/health','http://ollama_ai:11434/api/version','http://ollama_ai:11434/api/ps'])
        self.assertNotIn('SECRET',output.call_args.args[0])

    def test_positive_ids_reject_sql_and_invalid_values(self):
        for value in ['-1','0','8;DROP TABLE x','not-an-id',str(2**63)]:
            with self.assertRaises(argparse.ArgumentTypeError):audit.positive(value)
        self.assertEqual(audit.positive('469'),469)

    def test_ollama_logs_can_be_read_from_stderr(self):
        with patch.object(audit.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'out',b'log')):
            self.assertEqual(audit.run(['docker','logs','synthetic'],include_stderr=True),'outlog')
            self.assertEqual(audit.run(['docker','inspect','synthetic']),'out')


if __name__=='__main__':unittest.main()

"""Integration suite. Requires a disposable database via GAB_TEST_DATABASE_URL.
Never points at DATABASE_URL or initializes a production database.
"""
import os
import tempfile
import unittest
from pathlib import Path

URL = os.environ.get('GAB_TEST_DATABASE_URL')


@unittest.skipUnless(URL, 'Set GAB_TEST_DATABASE_URL to an empty disposable PostgreSQL database')
class ImportApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.conn = psycopg.connect(URL, autocommit=True)
        if cls.conn.execute("SELECT to_regclass('public.gab_crm_import_rows')").fetchone()[0]:
            raise RuntimeError('Tests require an empty database')
        root = Path(__file__).resolve().parents[1]
        cls.conn.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='gab_review_user') THEN CREATE ROLE gab_review_user; END IF; END $$")
        for path in ['app/schema.sql', 'app/schema_crm.sql', 'app/schema_mailings.sql', 'app/schema_notifications.sql',
                     'migrations/20261116_investor_import.sql', 'migrations/20261115_import_enrichment.sql',
                     'migrations/20261118_contacts_import_audit.sql', 'migrations/20261119_import_editor.sql',
                     'migrations/20261119_import_editor.sql', 'migrations/20261119_import_progress.sql', 'migrations/20261119_import_page_cache.sql', 'migrations/20261119_import_page_cache.sql', 'migrations/20261120_import_fragments.sql', 'migrations/20261120_import_fragments.sql','migrations/20261120_import_adaptive_splits.sql','migrations/20261120_import_adaptive_splits.sql']:
            cls.conn.execute((root / path).read_text())
        os.environ['DATABASE_URL'] = URL
        os.environ['GAB_APP_USER'] = 'synthetic-reviewer'
        os.environ['GAB_APP_PASSWORD'] = 'synthetic-test-password'
        cls.storage = tempfile.TemporaryDirectory()
        os.environ['GAB_IMPORT_STORAGE'] = cls.storage.name
        from app.main import app
        from app import investor_imports_api
        from unittest.mock import patch
        cls.storage_patch=patch.object(investor_imports_api,'STORAGE_DIR',Path(cls.storage.name))
        cls.storage_patch.start()
        from fastapi.testclient import TestClient
        cls.client = TestClient(app)
        cls.client.auth = ('synthetic-reviewer', 'synthetic-test-password')

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.conn.close()
        cls.storage_patch.stop()
        cls.storage.cleanup()

    def test_edit_retry_and_audit(self):
        c = self.client
        base = '/api/mailings/imports'
        response = c.post(base, files={'file': ('synthetic.pdf', b'%PDF-1.4\nsynthetic fixture', 'application/pdf')})
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()['item']['id']
        url = f'{base}/{batch}'
        claim = c.post(url + '/claim').json()
        self.assertTrue(claim['claimed'])
        token = claim['item']['worker_token']
        self.assertFalse(c.post(url + '/claim').json()['claimed'])
        payload = {'worker_token': token, 'records': [{'source_key':'row-1', 'company_name':'Synthetic Company',
                              'email':'person@example.test', 'revenue_rub':120000000}]}
        self.assertEqual(c.post(url + '/rows', json=payload).status_code, 200)
        rows = c.get(url + '/rows').json()['items']
        self.assertEqual(len(rows), 1)
        row_id = rows[0]['id']
        row_url = url + f'/rows/{row_id}'
        version = rows[0]['edit_version']
        edited = c.patch(row_url, json={'version':version, 'changes':{'company_name':'Manually corrected', 'email':'corrected@example.test', 'revenue_rub':'10'}})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()['item']['revenue_status'], 'below_threshold')
        self.assertEqual(c.patch(row_url, json={'version':version, 'changes':{'city':'Lost update'}}).status_code, 409)
        history = c.get(row_url + '/edits').json()['items']
        self.assertEqual(history[0]['actor'], 'synthetic-reviewer')
        self.assertEqual(history[0]['changes']['company_name']['before'], 'Synthetic Company')
        self.assertEqual(c.post(url + '/rows', json=payload).status_code, 200)
        rows = c.get(url + '/rows').json()['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['company_name'], 'Manually corrected')
        self.assertEqual(rows[0]['emails'], ['corrected@example.test'])
        self.assertEqual(rows[0]['raw_data']['company_name'], 'Synthetic Company')
        self.assertEqual(rows[0]['revenue_status'], 'below_threshold')
        self.assertEqual(c.patch(row_url, json={'version':rows[0]['edit_version'], 'changes':{'email':'invalid'}}).status_code, 422)
        self.assertEqual(c.get(row_url + '/match-preview').status_code, 200)
        confirm = c.post(row_url + '/confirm')
        self.assertEqual(confirm.status_code, 200, confirm.text)
        self.assertIsNone(self.conn.execute('SELECT investor_id FROM gab_crm_import_rows WHERE id=%s', [row_id]).fetchone()[0])
        self.assertEqual(c.get(url + '/file').content, b'%PDF-1.4\nsynthetic fixture')

    def test_page_cache_survives_retry_and_fences_old_worker(self):
        c=self.client
        batch=c.post('/api/mailings/imports',files={'file':('cache.pdf',b'%PDF-1.4 synthetic','application/pdf')}).json()['item']['id']
        url=f'/api/mailings/imports/{batch}'
        token=c.post(url+'/claim').json()['item']['worker_token']
        headers={'X-GAB-Worker-Token':token}
        payload={'source_page':1,'request_hash':'a'*64}
        def call(data=payload,head=headers):
            return c.post(url+'/page-result',json=data,headers=head,auth=None)
        self.assertEqual(c.post(url+'/page-result',json=payload,auth=None).status_code,401)
        self.assertIsNone(call().json()['result'])
        result={'done':True,'message':{'content':'{"records":[{"company_name":"Synthetic"}]}'}}
        self.assertEqual(call({**payload,'result':result}).status_code,200)
        self.assertEqual(call().json()['result'],result)
        self.assertIsNone(call({**payload,'request_hash':'b'*64}).json()['result'])
        self.assertEqual(call({**payload,'result':{'done':True,'message':{'content':'broken'}}}).status_code,422)
        self.assertEqual(c.post(url+'/fail',json={'worker_token':token,'error':'Synthetic timeout page 2'}).status_code,200)
        self.assertEqual(call().status_code,409)
        self.assertEqual(c.post(url+'/retry').status_code,200)
        newtoken=c.post(url+'/claim').json()['item']['worker_token']
        self.assertNotEqual(token,newtoken)
        self.assertEqual(call().status_code,409)
        self.assertEqual(call(head={'X-GAB-Worker-Token':newtoken}).json()['result'],result)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM gab_crm_import_rows WHERE batch_id=%s',[batch]).fetchone()[0],0)
        self.assertEqual(c.post(url+'/cancel').status_code,200)
        self.assertEqual(call({**payload,'result':result},head={'X-GAB-Worker-Token':newtoken}).status_code,409)

    def test_fragment_scope_progress_and_retry_preserve_page_and_fragments(self):
        c=self.client
        batch=c.post('/api/mailings/imports',files={'file':('fragments.pdf',b'%PDF-1.4 synthetic','application/pdf')}).json()['item']['id']
        url=f'/api/mailings/imports/{batch}'
        token=c.post(url+'/claim').json()['item']['worker_token']
        headers={'X-GAB-Worker-Token':token}
        result={'done':True,'message':{'content':'{"records":[]}'}}
        def cache(data,head=headers):return c.post(url+'/page-result',json=data,headers=head,auth=None)
        page={'source_page':7,'request_hash':'a'*64,'result':result}
        self.assertEqual(cache(page).status_code,200)
        fragment={**page,'scope':'fragment'}
        self.assertEqual(cache(fragment).status_code,200)
        self.assertEqual(cache({**fragment,'request_hash':'b'*64}).status_code,200)
        plan={'version':1,'children':['c'*64,'d'*64]}
        split={**fragment,'scope':'split','result':plan}
        self.assertEqual(cache(split).status_code,200)
        self.assertEqual(cache({**split,'result':None}).json()['result'],plan)
        self.assertEqual(cache({**split,'result':result}).status_code,422)
        self.assertEqual(cache({**split,'result':{**plan,'partial_text':'not allowed'}}).status_code,422)
        self.conn.execute((Path(__file__).resolve().parents[1]/'migrations/20261120_import_adaptive_splits.sql').read_text())
        self.assertEqual(cache({**split,'result':None}).json()['result'],plan)
        self.conn.execute((Path(__file__).resolve().parents[1]/'migrations/20261120_import_fragments.sql').read_text())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM gab_crm_import_page_fragments WHERE batch_id=%s',[batch]).fetchone()[0],2)
        self.assertEqual(cache({**page,'result':None}).json()['result'],result)
        self.assertEqual(cache({**fragment,'scope':'fragment;DELETE'}).status_code,422)
        body={'stage':'llm','total':8,'fragment':{'page':7,'completed':1,'total':3}}
        response=c.post(url+'/progress',headers=headers,auth=None,json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(c.get(url+'/progress').json()['fragment'],body['fragment'])
        part={**body,'fragment':{**body['fragment'],'part':'2.1'}}
        self.assertEqual(c.post(url+'/progress',headers=headers,auth=None,json=part).status_code,200)
        self.assertEqual(c.get(url+'/progress').json()['fragment']['part'],'2.1')
        self.assertEqual(c.post(url+'/progress',headers=headers,auth=None,json={**body,'fragment':{**body['fragment'],'part':'<bad>'}}).status_code,422)
        bad={**body,'fragment':{'page':7,'completed':4,'total':3}}
        self.assertEqual(c.post(url+'/progress',headers=headers,auth=None,json=bad).status_code,422)
        self.assertEqual(c.post(url+'/cancel').status_code,200)
        self.assertEqual(cache(fragment).status_code,409)
        self.assertEqual(c.post(url+'/retry').status_code,200)
        newtoken=c.post(url+'/claim').json()['item']['worker_token']
        self.assertIsNone(c.get(url+'/progress').json()['fragment'])
        self.assertEqual(cache({**fragment,'result':None},head={'X-GAB-Worker-Token':newtoken}).json()['result'],result)
        self.assertEqual(cache({**split,'result':None},head={'X-GAB-Worker-Token':newtoken}).json()['result'],plan)
        self.assertEqual(cache(split).status_code,409)
        self.assertEqual(cache({**fragment,'result':None}).status_code,409)
        self.assertEqual(c.post(url+'/cancel').status_code,200)

    def test_manual_confirmation_is_idempotent(self):
        c = self.client
        conn = self.conn
        batch = c.post('/api/mailings/imports', files={'file': ('confirm.pdf', b'%PDF-1.4 synthetic', 'application/pdf')}).json()['item']['id']
        url = f'/api/mailings/imports/{batch}'
        token = c.post(url + '/claim').json()['item']['worker_token']
        record = {'company_name':'Synthetic Confirmation Company', 'inn':'1234567894',
                  'full_name':'Synthetic Person', 'phone':'+1 202 555 0198',
                  'revenue_rub':150000000, 'revenue_year':2025, 'revenue_source':'synthetic document'}
        r = c.post(url + '/rows', json={'worker_token':token, 'records':[record]})
        self.assertEqual(r.status_code, 200, r.text)
        row = c.get(url + '/rows').json()['items'][0]
        row_url = url + f"/rows/{row['id']}"
        preview = c.get(row_url + '/match-preview').json()
        self.assertIn('extraction_warnings_require_manual_review', preview['reasons'])
        before = conn.execute('SELECT count(*) FROM gab_crm_investors').fetchone()[0]
        from concurrent.futures import ThreadPoolExecutor
        payload = {'version':preview['version'], 'acknowledged_warnings':['extraction_warnings_require_manual_review']}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: c.post(row_url + '/confirm', json=payload), range(2)))
        self.assertTrue(any(r.json().get('already_confirmed') for r in responses))
        result = next(r for r in responses if not r.json().get('already_confirmed'))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['status'], 'created', result.text)
        again = c.post(row_url + '/confirm')
        self.assertTrue(again.json()['already_confirmed'])
        self.assertEqual(conn.execute('SELECT count(*) FROM gab_crm_investors').fetchone()[0], before+1)
        self.assertEqual(c.patch(row_url, json={'version':0, 'changes':{'city':'blocked'}}).status_code, 409)
        c.post(url + '/rows', json={'worker_token':token, 'records':[{**record, 'company_name':'Changed by OCR'}]})
        self.assertEqual(c.get(url + '/rows').json()['items'][0]['company_name'], record['company_name'])
        event = conn.execute("SELECT detail_json FROM gab_crm_import_events WHERE import_row_id=%s AND event_type='confirmed_created'", [row['id']]).fetchone()[0]
        self.assertEqual(event['actor'], 'synthetic-reviewer')
        self.assertEqual(event['acknowledged_warnings'], ['extraction_warnings_require_manual_review'])

    def test_duplicate_candidates_by_inn_email_phone(self):
        c = self.client
        company = self.conn.execute("INSERT INTO gab_crm_companies(company_name,inn) VALUES ('Synthetic Dedupe','9876543210') RETURNING id").fetchone()[0]
        # Generate a synthetic checksum-valid INN distinct from other fixtures.
        prefix='987654321'
        inn=prefix + str(sum(int(x)*w for x,w in zip(prefix,(2,4,10,3,5,9,4,6,8))) % 11 % 10)
        self.conn.execute('UPDATE gab_crm_companies SET inn=%s WHERE id=%s', [inn,company])
        contact=self.conn.execute("INSERT INTO gab_crm_contacts(company_id,full_name,email,phone) VALUES (%s,'Synthetic Dedupe Person','dedupe@example.test','+1 202 555 0177') RETURNING id",[company]).fetchone()[0]
        for field, value in [('inn',inn),('email','dedupe@example.test'),('phone','+1 (202) 555-0177')]:
            with self.subTest(field=field):
                batch=c.post('/api/mailings/imports',files={'file':('dedupe.pdf',b'%PDF-1.4 synthetic','application/pdf')}).json()['item']['id']
                url=f'/api/mailings/imports/{batch}'
                token=c.post(url+'/claim').json()['item']['worker_token']
                record={'company_name':'Synthetic Dedupe', 'full_name':'Synthetic Dedupe Person',field:value}
                self.assertEqual(c.post(url+'/rows',json={'worker_token':token,'records':[record]}).status_code,200)
                row=c.get(url+'/rows').json()['items'][0]
                data=c.get(url+f"/rows/{row['id']}/match-preview").json()
                if field=='inn':
                    self.assertEqual(data['suggested_company_id'],company)
                else:
                    self.assertIn(contact,[item['id'] for item in data['contact_candidates']])

    def test_progress_callback_and_cancellation_fence(self):
        c=self.client;base='/api/mailings/imports'
        batch=c.post(base,files={'file':('synthetic-progress.pdf',b'%PDF-1.4 synthetic','application/pdf')}).json()['item']['id']
        url=f'{base}/{batch}'
        claim=c.post(url+'/claim',json={'execution_id':'125'}).json()
        token=claim['item']['worker_token']
        headers={'X-GAB-Worker-Token':token}
        self.assertEqual(c.get(url+'/progress',auth=None).status_code,401)
        self.assertEqual(c.post(url+'/cancel',headers=headers,auth=None).status_code,401)
        self.assertEqual(c.post(url+'/progress',json={'stage':'ocr','total':3,'page_completed':1},headers={'X-GAB-Worker-Token':'0'*32},auth=None).status_code,409)
        for page,done in [(2,1),(2,1),(1,2)]:
            response=c.post(url+'/progress',json={'stage':'ocr','total':3,'page_completed':page},headers=headers,auth=None)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['completed'],done)
        self.assertEqual(c.get(url+'/progress').json()['execution_id'],'125')
        self.assertTrue(c.post(url+'/cancel').json()['cancelled'])
        self.assertTrue(c.post(url+'/cancel').json()['already_cancelled'])
        self.assertEqual(c.post(url+'/progress',json={'stage':'llm','total':3},headers=headers,auth=None).status_code,409)
        self.assertEqual(c.post(url+'/rows',json={'worker_token':token,'records':[{'company_name':'Late synthetic'}]}).status_code,409)
        self.assertEqual(c.get(url+'/file').content,b'%PDF-1.4 synthetic')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM gab_crm_import_batch_events WHERE batch_id=%s',[batch]).fetchone()[0],1)

    def test_progress_finished_document_cannot_be_cancelled(self):
        c=self.client
        batch=c.post('/api/mailings/imports',files={'file':('finished.pdf',b'%PDF-1.4 synthetic','application/pdf')}).json()['item']['id']
        url=f'/api/mailings/imports/{batch}';token=c.post(url+'/claim').json()['item']['worker_token']
        c.post(url+'/rows',json={'worker_token':token,'records':[{'company_name':'Synthetic completed'}]})
        self.assertEqual(c.post(url+'/cancel').status_code,409)
        self.assertEqual(c.get(url+'/progress').json()['status'],'review')

    def test_retry_invalidates_worker(self):
        c = self.client
        base = '/api/mailings/imports'
        batch = c.post(base, files={'file': ('retry.pdf', b'%PDF-1.4 synthetic', 'application/pdf')}).json()['item']['id']
        url = base + f'/{batch}'
        old = c.post(url + '/claim').json()['item']['worker_token']
        self.assertTrue(c.post(url + '/fail', json={'worker_token':old, 'error':'synthetic failure'}).json()['updated'])
        self.assertEqual(c.post(url + '/retry').status_code, 200)
        new = c.post(url + '/claim').json()['item']['worker_token']
        self.assertNotEqual(new, old)
        self.assertEqual(c.post(url + '/rows', json={'worker_token':old, 'records':[{'company_name':'Stale'}]}).status_code, 409)
        self.assertFalse(c.post(url + '/fail', json={'worker_token':old}).json()['updated'])

    def test_file_validation(self):
        c = self.client
        for name, data, mime in [('wrong.pdf', b'not pdf', 'application/pdf'),
                                 ('wrong.xlsx', b'not zip', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                                 ('wrong.pdf', b'%PDF-1.4', 'image/png')]:
            with self.subTest(name=name):
                self.assertEqual(c.post('/api/mailings/imports', files={'file':(name,data,mime)}).status_code, 422)

    def test_deploy_health_program_uses_real_crm_routes(self):
        import io
        import sys
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from urllib.error import HTTPError
        from urllib.parse import urlsplit
        scripts=Path(__file__).resolve().parents[1]/'scripts'
        sys.path.insert(0,str(scripts))
        from deploy_runtime import HEALTH_PROGRAM
        visited=[]
        def open_route(request, timeout):
            path=urlsplit(request.full_url).path
            query=urlsplit(request.full_url).query
            visited.append(path)
            self.assertIn('Authorization',request.headers)
            response=self.client.get(path+('?' + query if query else ''))
            if response.status_code!=200:
                raise HTTPError(request.full_url,response.status_code,'unexpected health route',{},None)
            return io.BytesIO(response.content)
        for kind in ('crm','crm-basic'):
            with self.subTest(kind=kind),patch.dict(os.environ,{'GAB_HEALTH_KIND':kind}), \
                 patch('urllib.request.urlopen',side_effect=open_route),redirect_stdout(io.StringIO()):
                exec(compile(HEALTH_PROGRAM,'deployment-health','exec'),{})
        self.assertIn('/api/health',visited)
        self.assertNotIn('/health',visited)

    def test_z_reapply_migration_preserves_records_and_indexes(self):
        root=Path(__file__).resolve().parents[1]
        before=self.conn.execute('SELECT id, to_jsonb(r) FROM gab_crm_import_rows r ORDER BY id').fetchall()
        self.assertTrue(before, 'Earlier synthetic tests must have populated rows')
        tables_before=self.conn.execute("SELECT oid, relname FROM pg_class WHERE relname IN ('gab_crm_import_rows','gab_crm_import_batches','gab_crm_import_edits','gab_crm_import_edits_row_idx') ORDER BY relname").fetchall()
        audit_before=self.conn.execute('SELECT count(*) FROM gab_crm_import_edits').fetchone()[0]
        self.conn.execute((root/'migrations/20261119_import_editor.sql').read_text())
        self.conn.execute((root/'migrations/20261119_import_editor.sql').read_text())
        self.assertEqual(before,self.conn.execute('SELECT id, to_jsonb(r) FROM gab_crm_import_rows r ORDER BY id').fetchall())
        self.assertEqual(tables_before,self.conn.execute("SELECT oid, relname FROM pg_class WHERE relname IN ('gab_crm_import_rows','gab_crm_import_batches','gab_crm_import_edits','gab_crm_import_edits_row_idx') ORDER BY relname").fetchall())
        self.assertEqual(audit_before,self.conn.execute('SELECT count(*) FROM gab_crm_import_edits').fetchone()[0])


if __name__ == '__main__':
    unittest.main()

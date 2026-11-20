"""Provider lookup and bulk import tests: isolated schema, mocked remote APIs."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

URL=os.environ.get('GAB_TEST_DATABASE_URL')


@unittest.skipUnless(URL,'Requires a disposable local PostgreSQL database')
class ImportIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row
        cls.admin=psycopg.connect(URL,autocommit=True)
        cls.admin.execute('CREATE SCHEMA gab_integration_test')
        cls.admin.execute('SET search_path TO gab_integration_test,public')
        cls.admin.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='gab_review_user') THEN CREATE ROLE gab_review_user; END IF; END $$")
        root=Path(__file__).resolve().parents[1]
        for name in ['app/schema.sql','app/schema_crm.sql','app/schema_mailings.sql','app/schema_notifications.sql',
                     'migrations/20261116_investor_import.sql','migrations/20261115_import_enrichment.sql',
                     'migrations/20261118_contacts_import_audit.sql','migrations/20261119_import_editor.sql',
                     'migrations/20261119_import_progress.sql','migrations/20261119_import_page_cache.sql','migrations/20261120_import_fragments.sql']:
            cls.admin.execute((root/name).read_text())
        os.environ.update(DATABASE_URL=URL,GAB_APP_USER='synthetic-reviewer',GAB_APP_PASSWORD='synthetic-test-password')
        from app.main import app
        from app import import_enrichment,import_confirmation,import_bulk,investor_imports_api
        from fastapi.testclient import TestClient
        cls.client=TestClient(app);cls.client.auth=('synthetic-reviewer','synthetic-test-password')
        cls.storage=tempfile.TemporaryDirectory();cls.seq=0
        def conn():return psycopg.connect(URL,row_factory=dict_row,options='-c search_path=gab_integration_test,public')
        cls.patches=[patch.object(module,'get_conn',conn) for module in (import_enrichment,import_confirmation,import_bulk,investor_imports_api)]
        cls.patches.append(patch.object(investor_imports_api,'STORAGE_DIR',Path(cls.storage.name)))
        for p in cls.patches:p.start()

    @classmethod
    def tearDownClass(cls):
        for p in reversed(cls.patches):p.stop()
        cls.client.close();cls.storage.cleanup()
        cls.admin.execute('DROP SCHEMA gab_integration_test CASCADE');cls.admin.close()

    def batch(self,rows=1,warnings=False):
        cls=type(self);cls.seq+=1
        base='/api/mailings/imports'
        batch=self.client.post(base,files={'file':('synthetic.pdf',b'%PDF-1.4 artificial','application/pdf')}).json()['item']['id']
        url=f'{base}/{batch}'
        token=self.client.post(url+'/claim').json()['item']['worker_token']
        records=[]
        for i in range(rows):
            digits='99'+str(cls.seq*100+i).zfill(7)
            inn=digits+str(sum(int(digits[n])*w for n,w in enumerate((2,4,10,3,5,9,4,6,8)))%11%10)
            records.append({'source_key':str(i),'company_name':f'Synthetic business {cls.seq}-{i}','inn':inn,
                'phone':f'+7999{cls.seq:04}{i:03}','revenue_rub':150000000,'revenue_year':2025,'revenue_source':'synthetic',
                'warnings':['Synthetic OCR warning'] if warnings else []})
        result=self.client.post(url+'/rows',json={'worker_token':token,'records':records})
        self.assertEqual(result.status_code,200,result.text)
        return url,self.client.get(url+'/rows').json()['items']

    def test_bulk_preview_does_not_create_and_confirmation_is_manual_idempotent(self):
        url,rows=self.batch(2)
        before=self.admin.execute('SELECT count(*) FROM gab_crm_investors').fetchone()[0]
        result=self.client.post(url+'/confirm-preview',json={'row_ids':[r['id'] for r in rows]})
        self.assertEqual(result.status_code,200,result.text)
        preview=result.json();self.assertTrue(all(r['ready'] for r in preview['items']))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM gab_crm_investors').fetchone()[0],before)
        self.assertEqual(self.client.post(url+'/confirm-all',json={'token':preview['token']}).status_code,422)
        self.assertEqual(self.client.post(url+'/confirm-all',json={'token':preview['token']+'bad','reviewed':True}).status_code,409)
        first=self.client.post(url+'/confirm-all',json={'token':preview['token'],'reviewed':True,'acknowledge_warnings':True})
        self.assertEqual(first.status_code,200,first.text);self.assertEqual(first.json()['transferred'],2,first.text+str(preview['items']))
        second=self.client.post(url+'/confirm-all',json={'token':preview['token'],'reviewed':True})
        self.assertEqual(second.json()['transferred'],0)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM gab_crm_investors').fetchone()[0],before+2)

    def test_bulk_stale_preview_and_warning_review(self):
        url,rows=self.batch(warnings=True);row=rows[0]
        preview=self.client.post(url+'/confirm-preview',json={'row_ids':[row['id']]}).json()
        self.assertTrue(preview['items'][0]['ready'])
        skipped=self.client.post(url+'/confirm-all',json={'token':preview['token'],'reviewed':True}).json()
        self.assertEqual(skipped['transferred'],0)
        edit=self.client.patch(url+f"/rows/{row['id']}",json={'version':row['edit_version'],'changes':{'city':'Synthetic revised'}})
        self.assertEqual(edit.status_code,200,edit.text)
        stale=self.client.post(url+'/confirm-all',json={'token':preview['token'],'reviewed':True,'acknowledge_warnings':True})
        self.assertEqual(stale.json()['transferred'],0)
        preview=self.client.post(url+'/confirm-preview',json={'row_ids':[row['id']]}).json()
        result=self.client.post(url+'/confirm-all',json={'token':preview['token'],'reviewed':True,'acknowledge_warnings':True})
        self.assertEqual(result.json()['transferred'],1,result.text)

    def test_dadata_checko_are_candidates_until_explicit_approval(self):
        url,rows=self.batch();row=rows[0];inn=row['inn'];base=f"/api/mailings/registry-enrichment/imports/{row['id']}"
        suggestion={'suggestions':[{'value':row['company_name'],'data':{'inn':inn,'type':'LEGAL','name':{'full_with_opf':row['company_name']},'emails':[{'value':'dadata@example.test'}]}}]}
        checko={'ИНН':inn,'НаимПолн':row['company_name'],'Контакты':{'Емэйл':['checko@example.test'],'Тел':['+79990000999']}}
        with patch('app.import_enrichment.dadata_request',return_value=suggestion) as search,patch('app.import_enrichment.checko_request',return_value=checko) as card,patch('app.import_enrichment.find_latest_revenue',return_value=(2025,200000000)):
            found=self.client.post(base+'/search',json={})
            self.assertEqual(found.status_code,200,found.text)
            search.assert_called_once_with(inn)
            self.assertEqual(found.json()['candidates'][0]['candidate']['emails'],['dadata@example.test'])
            detail=self.client.post(base+'/checko',json={'inn':inn})
            self.assertEqual(detail.status_code,200,detail.text)
            card.assert_called_once_with('company',inn)
            self.assertEqual(self.client.get(url+'/rows').json()['items'][0]['enriched_emails'],[])
            apply=self.client.post(base+'/confirm',json={'inn':inn,'version':row['edit_version'],'use_revenue':True,'include_dadata':True,'dadata_checked_at':found.json()['candidates'][0]['candidate']['checked_at']})
            self.assertEqual(apply.status_code,200,apply.text)
        actual=self.client.get(url+'/rows').json()['items'][0]
        self.assertEqual(set(actual['enriched_emails']),{'dadata@example.test','checko@example.test'})
        self.assertIsNone(actual['investor_id'])
        self.assertEqual(float(actual['revenue_rub']),200000000)
        self.assertGreater(actual['edit_version'],row['edit_version'])

    def test_wrong_checko_inn_is_rejected_and_manual_revenue_survives(self):
        url,rows=self.batch();row=rows[0];inn=row['inn'];base=f"/api/mailings/registry-enrichment/imports/{row['id']}"
        suggestion={'suggestions':[{'value':row['company_name'],'data':{'inn':inn,'type':'LEGAL'}}]}
        with patch('app.import_enrichment.dadata_request',return_value=suggestion):
            self.client.post(base+'/search',json={})
        with patch('app.import_enrichment.checko_request',return_value={'ИНН':'7707083893'}):
            self.assertEqual(self.client.post(base+'/checko',json={'inn':inn}).status_code,502)
        edited=self.client.patch(url+f"/rows/{row['id']}",json={'version':row['edit_version'],'changes':{'revenue_rub':175000000}}).json()['item']
        with patch('app.import_enrichment.checko_request',return_value={'ИНН':inn}),patch('app.import_enrichment.find_latest_revenue',return_value=(2025,250000000)):
            self.client.post(base+'/checko',json={'inn':inn})
            applied=self.client.post(base+'/confirm',json={'inn':inn,'version':edited['edit_version'],'use_revenue':True})
            self.assertEqual(applied.status_code,200,applied.text)
            self.assertEqual(float(applied.json()['import_row']['revenue_rub']),175000000)

    def test_bulk_financial_scope_expiry_and_auth_guards(self):
        from app.import_bulk import sign
        url,rows=self.batch();row=rows[0]
        self.client.patch(url+f"/rows/{row['id']}",json={'version':row['edit_version'],'changes':{'revenue_rub':10}})
        preview=self.client.post(url+'/confirm-preview',json={'row_ids':[row['id']]}).json()
        self.assertFalse(preview['items'][0]['ready'])
        result=self.client.post(url+'/confirm-all',json={'token':preview['token'],'reviewed':True,'acknowledge_warnings':True})
        self.assertEqual(result.json()['transferred'],0)
        other,_=self.batch()
        self.assertEqual(self.client.post(other+'/confirm-all',json={'token':preview['token'],'reviewed':True}).status_code,409)
        expired=sign({'batch':int(url.rsplit('/',1)[1]),'issued':0,'items':[]})
        self.assertEqual(self.client.post(url+'/confirm-all',json={'token':expired,'reviewed':True}).status_code,409)
        self.assertEqual(self.client.get('/api/mailings/registry-enrichment/imports/configuration',auth=None).status_code,401)
        self.assertEqual(self.client.post(url+'/confirm-all',auth=None,headers={'X-GAB-Worker-Token':'a'*32},json={'token':preview['token'],'reviewed':True}).status_code,401)

    def test_registry_card_refresh_and_identity_edit_require_new_review(self):
        url,rows=self.batch();row=rows[0];inn=row['inn'];base=f"/api/mailings/registry-enrichment/imports/{row['id']}"
        suggestion={'suggestions':[{'value':row['company_name'],'data':{'inn':inn,'type':'LEGAL'}}]}
        with patch('app.import_enrichment.dadata_request',return_value=suggestion),patch('app.import_enrichment.checko_request',return_value={'ИНН':inn,'Контакты':{'Емэйл':['company@example.test']}}),patch('app.import_enrichment.find_latest_revenue',return_value=(2025,200000000)):
            self.client.post(base+'/search',json={})
            first=self.client.post(base+'/checko',json={'inn':inn}).json()['item']
            second=self.client.post(base+'/checko',json={'inn':inn}).json()['item']
            stale=self.client.post(base+'/confirm',json={'inn':inn,'version':row['edit_version'],'checked_at':first['checked_at']})
            self.assertEqual(stale.status_code,409,stale.text)
            result=self.client.post(base+'/confirm',json={'inn':inn,'version':row['edit_version'],'checked_at':second['checked_at'],'use_revenue':True})
            self.assertEqual(result.status_code,200,result.text)
        current=self.client.get(url+'/rows').json()['items'][0]
        edited=self.client.patch(url+f"/rows/{row['id']}",json={'version':current['edit_version'],'changes':{'company_name':'Changed synthetic identity'}})
        self.assertEqual(edited.status_code,200,edited.text)
        current=self.client.get(url+'/rows').json()['items'][0]
        self.assertEqual(current['enriched_emails'],[])
        self.assertIsNone(current['revenue_rub'])

    def test_dadata_only_approval_does_not_require_checko_key(self):
        url,rows=self.batch();row=rows[0];inn=row['inn'];base=f"/api/mailings/registry-enrichment/imports/{row['id']}"
        suggestion={'suggestions':[{'value':row['company_name'],'data':{'inn':inn,'type':'LEGAL','emails':[{'value':'only-dadata@example.test'}]}}]}
        with patch('app.import_enrichment.dadata_request',return_value=suggestion):
            card=self.client.post(base+'/search',json={}).json()['candidates'][0]['candidate']
        result=self.client.post(base+'/confirm',json={'inn':inn,'provider':'dadata','version':row['edit_version'],'checked_at':card['checked_at']})
        self.assertEqual(result.status_code,200,result.text)
        current=self.client.get(url+'/rows').json()['items'][0]
        self.assertEqual(current['enriched_emails'],['only-dadata@example.test'])
        self.assertIsNone(current['investor_id'])

    def test_parallel_bulk_clicks_do_not_create_duplicate_cards(self):
        from concurrent.futures import ThreadPoolExecutor
        url,rows=self.batch(2)
        preview=self.client.post(url+'/confirm-preview',json={'row_ids':[r['id'] for r in rows]}).json()
        before=self.admin.execute('SELECT count(*) FROM gab_crm_investors').fetchone()[0]
        payload={'token':preview['token'],'reviewed':True,'acknowledge_warnings':True}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:self.client.post(url+'/confirm-all',json=payload),range(2)))
        self.assertTrue(all(r.status_code in (200,409) for r in responses))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM gab_crm_investors').fetchone()[0],before+2)

    def test_same_company_rows_reuse_cards_created_by_this_batch(self):
        url,rows=self.batch(2)
        self.admin.execute('UPDATE gab_crm_import_rows SET inn=%s,company_name=%s WHERE id=%s',[rows[0]['inn'],rows[0]['company_name'],rows[1]['id']])
        preview=self.client.post(url+'/confirm-preview',json={'row_ids':[r['id'] for r in rows]}).json()
        before=self.admin.execute('SELECT count(*) FROM gab_crm_investors').fetchone()[0]
        result=self.client.post(url+'/confirm-all',json={'token':preview['token'],'reviewed':True,'acknowledge_warnings':True})
        self.assertEqual(result.json()['transferred'],2,result.text)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM gab_crm_investors').fetchone()[0],before+1)

    def test_registry_fills_missing_company_without_overwriting_source_names(self):
        url,rows=self.batch();row=rows[0];inn=row['inn']
        self.admin.execute('UPDATE gab_crm_import_rows SET company_name=NULL,full_name=%s WHERE id=%s',['Synthetic proprietor',row['id']])
        current=self.client.get(url+'/rows').json()['items'][0]
        base=f"/api/mailings/registry-enrichment/imports/{row['id']}"
        suggestion={'suggestions':[{'value':'Synthetic registered company','data':{'inn':inn,'type':'LEGAL'}}]}
        with patch('app.import_enrichment.dadata_request',return_value=suggestion):
            card=self.client.post(base+'/search',json={}).json()['candidates'][0]['candidate']
        result=self.client.post(base+'/confirm',json={'inn':inn,'provider':'dadata','version':current['edit_version'],'checked_at':card['checked_at']})
        self.assertEqual(result.status_code,200,result.text)
        current=self.client.get(url+'/rows').json()['items'][0]
        self.assertEqual(current['company_name'],'Synthetic registered company')
        self.assertEqual(current['full_name'],'Synthetic proprietor')

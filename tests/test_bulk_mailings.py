"""Synthetic isolated schema, mocked delivery only. Never contacts SMTP/n8n."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

URL=os.environ.get('GAB_TEST_DATABASE_URL')


@unittest.skipUnless(URL,'Set disposable GAB_TEST_DATABASE_URL')
class BulkMailingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row
        cls.admin=psycopg.connect(URL,autocommit=True)
        cls.admin.execute('CREATE SCHEMA gab_bulk_test')
        cls.admin.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='gab_review_user') THEN CREATE ROLE gab_review_user; END IF; END $$")
        cls.admin.execute('SET search_path TO gab_bulk_test,public')
        root=Path(__file__).resolve().parents[1]
        for name in ['app/schema.sql','app/schema_crm.sql','app/schema_mailings.sql',
                     'migrations/20261116_investor_import.sql','migrations/20261118_contacts_import_audit.sql',
                     'migrations/20261119_bulk_mailing.sql','migrations/20261119_bulk_mailing.sql']:
            cls.admin.execute((root/name).read_text())
        os.environ['DATABASE_URL']=URL
        os.environ['GAB_APP_USER']='synthetic-reviewer'
        os.environ['GAB_APP_PASSWORD']='synthetic-test-password'
        from app.main import app
        from app import mailings_api,mailing_bulk
        from fastapi.testclient import TestClient
        cls.bulk=mailing_bulk;cls.api=mailings_api
        cls.storage=tempfile.TemporaryDirectory()
        def connection():return psycopg.connect(URL,row_factory=dict_row,options='-c search_path=gab_bulk_test,public')
        cls.patches=[patch.object(mailings_api,'get_conn',connection),patch.object(mailing_bulk,'get_conn',connection),
                     patch.object(mailings_api,'STORAGE_DIR',Path(cls.storage.name)),
                     patch.object(mailings_api,'WEBHOOK_URL','https://n8n.techsmysl.ru/webhook/synthetic-do-not-call'),
                     patch.object(mailings_api,'WEBHOOK_TOKEN','synthetic-token'),
                     patch.object(mailing_bulk.time,'sleep'),
                     patch.object(mailing_bulk,'send_one',side_effect=AssertionError('Live delivery forbidden in tests'))]
        for p in cls.patches:p.start()
        cls.client=TestClient(app);cls.client.auth=('synthetic-reviewer','synthetic-test-password')

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        for p in reversed(cls.patches):p.stop()
        cls.admin.execute('DROP SCHEMA gab_bulk_test CASCADE')
        cls.admin.close();cls.storage.cleanup()

    def setUp(self):
        self.admin.execute('TRUNCATE gab_crm_mailing_runs,gab_crm_mailing_events,gab_crm_mailing_recipients,gab_crm_mailing_attachments,gab_crm_mailings,gab_crm_investors,gab_crm_contacts,gab_crm_companies CASCADE')

    def campaign(self):
        c=self.client
        m=c.post('/api/mailings/',json={'name':'Artificial campaign','subject':'Artificial subject','body_html':'<p>Artificial message</p>'}).json()['id']
        for i in range(2):
            investor=self.admin.execute("INSERT INTO gab_crm_investors(name) VALUES ('Synthetic investor') RETURNING id").fetchone()[0]
            self.admin.execute("INSERT INTO gab_crm_mailing_recipients(mailing_id,investor_id,recipient_name,email) VALUES (%s,%s,'Synthetic Person',%s)",[m,investor,f'person{i}@example.test'])
        return m,f'/api/mailings/{m}'

    def test_preview_never_sends(self):
        m,url=self.campaign()
        with patch.object(self.bulk,'send_one') as sender:
            plan=self.client.post(url+'/bulk-preview')
            self.assertEqual(plan.status_code,200,plan.text)
            self.assertEqual(plan.json()['count'],2)
            self.client.get(url+'/bulk-status')
            sender.assert_not_called()
        self.assertIsNone(self.admin.execute('SELECT bulk_run_id FROM gab_crm_mailings WHERE id=%s',[m]).fetchone()[0])

    def test_only_manual_confirmation_and_no_duplicate_launch(self):
        m,url=self.campaign();plan=self.client.post(url+'/bulk-preview').json()
        self.assertEqual(self.client.post(url+'/bulk-send',json={'preview_hash':plan['preview_hash']}).status_code,422)
        with patch.object(self.bulk,'send_one',return_value='synthetic-message-id') as sender:
            result=self.client.post(url+'/bulk-send',json={'confirm':True,'preview_hash':plan['preview_hash']})
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(sender.call_count,2)
            self.assertNotEqual(sender.call_args_list[0].args[0]['request_id'],sender.call_args_list[1].args[0]['request_id'])
            again=self.client.post(url+'/bulk-send',json={'confirm':True,'preview_hash':plan['preview_hash']})
            self.assertTrue(again.json()['already_started'])
            self.assertEqual(sender.call_count,2)
            status=self.client.get(url+'/bulk-status').json()
            self.assertEqual(status['counts']['sent'],2)
            self.assertEqual(status['status'],'completed')
        before=self.admin.execute('SELECT to_jsonb(r) FROM gab_crm_mailing_recipients r WHERE mailing_id=%s ORDER BY id',[m]).fetchall()
        self.admin.execute((Path(__file__).resolve().parents[1]/'migrations/20261119_bulk_mailing.sql').read_text())
        self.assertEqual(before,self.admin.execute('SELECT to_jsonb(r) FROM gab_crm_mailing_recipients r WHERE mailing_id=%s ORDER BY id',[m]).fetchall())
        self.assertEqual(self.client.patch(url,json={'name':'blocked','subject':'changed'}).status_code,409)
        self.assertEqual(self.client.post(url+'/recipients',json={}).status_code,409)

    def test_changed_plan_blocks_send(self):
        m,url=self.campaign();plan=self.client.post(url+'/bulk-preview').json()
        self.client.patch(url,json={'name':'Changed','subject':'Different','body_text':'Different'})
        with patch.object(self.bulk,'send_one') as sender:
            response=self.client.post(url+'/bulk-send',json={'confirm':True,'preview_hash':plan['preview_hash']})
            self.assertEqual(response.status_code,409,response.text)
            sender.assert_not_called()

    def test_timeout_pauses_and_manual_resume_never_retries_uncertain(self):
        m,url=self.campaign();plan=self.client.post(url+'/bulk-preview').json()
        with patch.object(self.bulk,'send_one',side_effect=TimeoutError) as sender:
            self.client.post(url+'/bulk-send',json={'confirm':True,'preview_hash':plan['preview_hash']})
            self.assertEqual(sender.call_count,1)
        status=self.client.get(url+'/bulk-status').json()
        self.assertEqual(status['status'],'paused')
        self.assertEqual(status['counts']['unknown'],1)
        self.assertEqual(status['counts']['pending'],1)
        first_email=next(r['email'] for r in status['recipients'] if r['delivery_state']=='unknown')
        with patch.object(self.bulk,'send_one',return_value='synthetic-id') as sender:
            response=self.client.post(url+'/bulk-resume',json={'confirm':True,'run_id':status['run_id']})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(sender.call_count,1)
            self.assertNotEqual(sender.call_args.args[0]['to_email'],first_email)
        status=self.client.get(url+'/bulk-status').json()
        self.assertEqual(status['counts']['sent'],1)
        self.assertEqual(status['counts']['unknown'],1)

    def test_interrupted_attempt_is_not_replayed(self):
        m,url=self.campaign();plan=self.client.post(url+'/bulk-preview').json()
        with patch.object(self.bulk,'dispatch') as worker:
            result=self.client.post(url+'/bulk-send',json={'confirm':True,'preview_hash':plan['preview_hash']}).json()
            worker.assert_called_once()
        self.admin.execute("UPDATE gab_crm_mailing_recipients SET delivery_state='sending' WHERE id=(SELECT min(id) FROM gab_crm_mailing_recipients WHERE mailing_id=%s)",[m])
        with patch.object(self.bulk,'send_one',return_value='synthetic-id') as sender:
            self.client.post(url+'/bulk-resume',json={'confirm':True,'run_id':result['run_id']})
            self.assertEqual(sender.call_count,1)
        self.assertEqual(self.client.get(url+'/bulk-status').json()['counts']['unknown'],1)

    def test_concurrent_double_click_only_dispatches_once(self):
        from concurrent.futures import ThreadPoolExecutor
        m,url=self.campaign();plan=self.client.post(url+'/bulk-preview').json()
        body={'confirm':True,'preview_hash':plan['preview_hash']}
        with patch.object(self.bulk,'send_one',return_value='synthetic-message') as sender:
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses=list(pool.map(lambda _:self.client.post(url+'/bulk-send',json=body),range(2)))
            self.assertTrue(any(r.status_code==200 for r in responses))
            self.assertTrue(all(r.status_code in (200,409) for r in responses))
            self.assertEqual(sender.call_count,2)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM gab_crm_mailing_events WHERE mailing_id=%s AND event_type='bulk_manually_confirmed'",[m]).fetchone()[0],1)

    def test_pause_stops_after_current_message(self):
        m,url=self.campaign();plan=self.client.post(url+'/bulk-preview').json()
        def accepted_and_pause(payload):
            run_id=self.admin.execute('SELECT id FROM gab_crm_mailing_runs WHERE mailing_id=%s',[m]).fetchone()[0]
            self.bulk.pause_manually(m,self.bulk.Resume(confirm=True,run_id=run_id))
            return 'synthetic-message'
        with patch.object(self.bulk,'send_one',side_effect=accepted_and_pause) as sender:
            self.client.post(url+'/bulk-send',json={'confirm':True,'preview_hash':plan['preview_hash']})
            self.assertEqual(sender.call_count,1)
        status=self.client.get(url+'/bulk-status').json()
        self.assertEqual(status['status'],'paused')
        self.assertEqual(status['counts']['sent'],1)
        self.assertEqual(status['counts']['pending'],1)

    def test_opt_out_and_invalid_addresses_excluded(self):
        m,url=self.campaign()
        other=self.client.post('/api/mailings/',json={'name':'Artificial suppression'}).json()['id']
        self.admin.execute("INSERT INTO gab_crm_mailing_recipients(mailing_id,email,status) VALUES (%s,'person0@example.test','opted_out')",[other])
        self.admin.execute("UPDATE gab_crm_mailing_recipients SET email='not an email' WHERE mailing_id=%s AND email='person1@example.test'",[m])
        result=self.client.post(url+'/bulk-preview').json()
        self.assertEqual(result['count'],0);self.assertFalse(result['can_send'])
        self.assertEqual(len(result['excluded']),2)

    def test_unresolved_template_is_blocked(self):
        m,url=self.campaign()
        self.client.patch(url,json={'name':'Artificial','subject':'Hello {{contact_name}}','body_text':'Artificial'})
        self.assertEqual(self.client.post(url+'/bulk-preview').status_code,422)

    def test_corrupted_attachment_blocks_sending(self):
        m,url=self.campaign()
        file=self.client.post(url+'/attachments',files={'file':('artificial.txt.pdf',b'%PDF artificial','application/pdf')})
        self.assertEqual(file.status_code,200,file.text)
        plan=self.client.post(url+'/bulk-preview').json()
        path=next((Path(self.storage.name)/str(m)).iterdir());path.write_bytes(b'corrupt')
        with patch.object(self.bulk,'send_one') as sender:
            self.assertEqual(self.client.post(url+'/bulk-send',json={'confirm':True,'preview_hash':plan['preview_hash']}).status_code,422)
            sender.assert_not_called()


class SenderContractTests(unittest.TestCase):
    def response(self,data):
        from unittest.mock import MagicMock
        response=MagicMock();response.status=200;response.read.return_value=__import__('json').dumps(data).encode()
        response.__enter__.return_value=response
        return response

    def test_validates_smtp_acceptance_and_exact_destination_without_network(self):
        from app.mailing_bulk import send_one
        payload={'request_id':'synthetic-id','mailing_id':999,'to_email':'person@example.test','subject':'Synthetic','body_text':'Synthetic','body_html':'','attachments':[]}
        result={'sent':True,'mailing_id':999,'to_email':'person@example.test','accepted':['person@example.test'],'rejected':[],'message_id':'synthetic-message'}
        with patch('app.mailing_bulk.transport_settings',return_value=('https://n8n.techsmysl.ru/webhook/synthetic','synthetic-token')), \
             patch('app.mailing_bulk.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=self.response(result)
            self.assertEqual(send_one(payload),'synthetic-message')
            for bad in [{**result,'to_email':'other@example.test'},{**result,'accepted':[]},{**result,'rejected':['person@example.test']},{**result,'mailing_id':1}]:
                opener.return_value.open.return_value=self.response(bad)
                with self.assertRaises(ValueError):send_one(payload)

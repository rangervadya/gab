"""Missing-contact filters, using transaction-local synthetic rows only."""
import contextlib
import os
import unittest


def check_missing_email_filters(api, conn):
    assert api.collect_import_contacts({'emails': [], 'notes': 'Rejected wrong@example.test',
        'field_sources': {'emails': 'registry@example.test'}, 'public_email_candidates': 'candidate@example.test'})[0] == []
    assert api.collect_import_contacts({'emails': ['right@example.test'], 'notes': 'old@example.test'})[0] == ['right@example.test']
    assert api.collect_import_contacts({'description': 'Legacy contact legacy@example.test'})[0] == ['legacy@example.test']
    for table in ('gab_crm_import_rows', 'gab_crm_import_enrichment'):
        conn.execute(f'CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING DEFAULTS) ON COMMIT DROP')
    conn.execute("""INSERT INTO gab_crm_import_rows(id,batch_id,source_key,company_name,phone,email,revenue_status)
        VALUES (901,991,'one','Unknown revenue','+70000000001',NULL,'unknown'),
               (902,991,'two','No phone',NULL,NULL,'unknown'),
               (903,991,'three','Known email',NULL,'known@example.test','unknown'),
               (904,992,'other','Other batch','+70000000002',NULL,'unknown')""")
    @contextlib.contextmanager
    def isolated():
        yield conn
    old = api.get_conn
    api.get_conn = isolated
    try:
        def ids(segment):
            return {r['id'] for r in api.list_import_rows(991, segment=segment)['items']}
        assert ids('without_email') == {901, 902}
        assert ids('without_email_phone') == {901}
        assert ids('without_email_no_phone') == {902}
        # An unconfirmed provider candidate must not hide a missing contact.
        conn.execute("""INSERT INTO gab_crm_import_enrichment(id,import_row_id,provider,entity_type,inn,emails,match_status)
            VALUES (905,901,'dadata','company','1234567890','["candidate@example.test"]','candidate')""")
        assert ids('without_email_phone') == {901}
        conn.execute("UPDATE gab_crm_import_enrichment SET match_status='confirmed' WHERE id=905")
        assert ids('without_email') == {902}
        # New imported rows appear; manual edits remove them without materialized lists.
        conn.execute("INSERT INTO gab_crm_import_rows(id,batch_id,source_key,company_name) VALUES (906,991,'new','New entry')")
        assert ids('without_email_no_phone') == {902, 906}
        conn.execute("UPDATE gab_crm_import_rows SET email='edited@example.test' WHERE id=906")
        assert ids('without_email_no_phone') == {902}
    finally:
        api.get_conn = old
        conn.rollback()


@unittest.skipUnless(os.environ.get('GAB_TEST_DATABASE_URL'), 'Disposable test DB required')
class MissingEmailTests(unittest.TestCase):
    def test_live_filters(self):
        import psycopg
        from psycopg.rows import dict_row
        from app import investor_imports_api
        with psycopg.connect(os.environ['GAB_TEST_DATABASE_URL'], row_factory=dict_row) as conn:
            check_missing_email_filters(investor_imports_api, conn)

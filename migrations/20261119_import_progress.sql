BEGIN;
SET LOCAL lock_timeout='10s';
SET LOCAL statement_timeout='120s';
ALTER TABLE gab_crm_import_batches
 ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ,
 ADD COLUMN IF NOT EXISTS processing_finished_at TIMESTAMPTZ,
 ADD COLUMN IF NOT EXISTS progress_stage TEXT,
 ADD COLUMN IF NOT EXISTS stage_started_at TIMESTAMPTZ,
 ADD COLUMN IF NOT EXISTS progress_total INTEGER,
 ADD COLUMN IF NOT EXISTS progress_pages JSONB NOT NULL DEFAULT '[]'::jsonb,
 ADD COLUMN IF NOT EXISTS progress_updated_at TIMESTAMPTZ,
 ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ,
 ADD COLUMN IF NOT EXISTS n8n_execution_id TEXT;
CREATE TABLE IF NOT EXISTS gab_crm_import_batch_events (
 id BIGSERIAL PRIMARY KEY,
 batch_id BIGINT NOT NULL REFERENCES gab_crm_import_batches(id),
 event_type TEXT NOT NULL,
 actor TEXT NOT NULL,
 detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS gab_crm_import_batch_events_idx ON gab_crm_import_batch_events(batch_id,created_at DESC);
GRANT SELECT,INSERT ON gab_crm_import_batch_events TO gab_review_user;
GRANT USAGE,SELECT ON SEQUENCE gab_crm_import_batch_events_id_seq TO gab_review_user;
COMMIT;

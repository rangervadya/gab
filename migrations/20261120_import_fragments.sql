BEGIN;
SET LOCAL lock_timeout='10s';
SET LOCAL statement_timeout='120s';
ALTER TABLE gab_crm_import_batches ADD COLUMN IF NOT EXISTS progress_detail JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE IF NOT EXISTS gab_crm_import_page_fragments (
 batch_id BIGINT NOT NULL REFERENCES gab_crm_import_batches(id),
 source_page INTEGER NOT NULL CHECK (source_page BETWEEN 1 AND 200),
 request_hash TEXT NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
 result_json JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(batch_id,source_page,request_hash)
);
GRANT SELECT,INSERT,UPDATE ON gab_crm_import_page_fragments TO gab_review_user;
COMMIT;

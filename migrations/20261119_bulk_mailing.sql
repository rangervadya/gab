BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
ALTER TABLE gab_crm_mailings ADD COLUMN IF NOT EXISTS bulk_run_id UUID;
ALTER TABLE gab_crm_mailing_recipients
    ADD COLUMN IF NOT EXISTS review_record_id TEXT,
    ADD COLUMN IF NOT EXISTS bulk_run_id UUID,
    ADD COLUMN IF NOT EXISTS delivery_request_id UUID,
    ADD COLUMN IF NOT EXISTS delivery_state TEXT NOT NULL DEFAULT 'pending';
CREATE TABLE IF NOT EXISTS gab_crm_mailing_runs (
    id UUID PRIMARY KEY,
    mailing_id BIGINT NOT NULL UNIQUE REFERENCES gab_crm_mailings(id),
    status TEXT NOT NULL CHECK (status IN ('running','paused','completed')),
    approved_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    worker_token UUID NOT NULL,
    started_by TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    error_message TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS gab_crm_mailing_request_unique
    ON gab_crm_mailing_recipients(delivery_request_id) WHERE delivery_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS gab_crm_mailing_delivery_idx
    ON gab_crm_mailing_recipients(bulk_run_id, delivery_state, id);
GRANT SELECT, INSERT, UPDATE ON gab_crm_mailing_runs TO gab_review_user;
COMMIT;

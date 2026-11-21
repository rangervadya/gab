BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
ALTER TABLE gab_crm_mailings
    ADD COLUMN IF NOT EXISTS recipient_scope TEXT NOT NULL DEFAULT 'all'
        CHECK (recipient_scope IN ('all', 'revenue_100m'));
ALTER TABLE gab_crm_mailing_recipients
    ADD COLUMN IF NOT EXISTS revenue_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;
COMMIT;

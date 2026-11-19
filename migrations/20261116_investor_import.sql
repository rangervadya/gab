BEGIN;

ALTER TABLE gab_crm_companies
    ADD COLUMN IF NOT EXISTS revenue_rub NUMERIC(20,2),
    ADD COLUMN IF NOT EXISTS revenue_year INTEGER,
    ADD COLUMN IF NOT EXISTS revenue_source TEXT,
    ADD COLUMN IF NOT EXISTS revenue_checked_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS gab_crm_import_batches (
    id BIGSERIAL PRIMARY KEY,
    folder_name TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    storage_filename TEXT NOT NULL,
    file_extension TEXT NOT NULL
        CHECK (file_extension IN ('pdf','xls','xlsx','doc','docx')),
    mime_type TEXT NOT NULL,
    file_size_bytes BIGINT NOT NULL DEFAULT 0,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (status IN (
            'uploaded','processing','review','ready','completed','failed'
        )),
    total_rows INTEGER NOT NULL DEFAULT 0,
    eligible_rows INTEGER NOT NULL DEFAULT 0,
    without_email_rows INTEGER NOT NULL DEFAULT 0,
    below_threshold_rows INTEGER NOT NULL DEFAULT 0,
    unknown_revenue_rows INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gab_crm_import_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL
        REFERENCES gab_crm_import_batches(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    source_locator TEXT,
    raw_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    company_name TEXT,
    inn TEXT,
    city TEXT,
    full_name TEXT,
    position TEXT,
    phone TEXT,
    email TEXT,
    revenue_rub NUMERIC(20,2),
    revenue_year INTEGER,
    revenue_source TEXT,
    revenue_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (revenue_status IN (
            'eligible','below_threshold','unknown','needs_review'
        )),
    import_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (import_status IN (
            'pending','created','matched','duplicate','needs_review','rejected'
        )),
    company_id BIGINT REFERENCES gab_crm_companies(id) ON DELETE SET NULL,
    contact_id BIGINT REFERENCES gab_crm_contacts(id) ON DELETE SET NULL,
    investor_id BIGINT REFERENCES gab_crm_investors(id) ON DELETE SET NULL,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, source_key),
    CHECK (
        revenue_status <> 'eligible'
        OR (revenue_rub IS NOT NULL AND revenue_rub >= 100000000)
    ),
    CHECK (
        revenue_status <> 'below_threshold'
        OR (revenue_rub IS NOT NULL AND revenue_rub < 100000000)
    )
);

ALTER TABLE gab_crm_mailings
    ADD COLUMN IF NOT EXISTS import_batch_id BIGINT
        REFERENCES gab_crm_import_batches(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS revenue_threshold_rub NUMERIC(20,2)
        NOT NULL DEFAULT 100000000;

ALTER TABLE gab_crm_mailing_recipients
    ADD COLUMN IF NOT EXISTS import_row_id BIGINT
        REFERENCES gab_crm_import_rows(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS phone TEXT,
    ADD COLUMN IF NOT EXISTS message_id TEXT,
    ADD COLUMN IF NOT EXISTS followup_count SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS followup_due_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS followup_sent_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS replied_at TIMESTAMPTZ;

ALTER TABLE gab_crm_mailing_recipients
    DROP CONSTRAINT IF EXISTS gab_crm_mailing_recipients_status_check;

ALTER TABLE gab_crm_mailing_recipients
    ADD CONSTRAINT gab_crm_mailing_recipients_status_check
    CHECK (status IN (
        'pending','no_email','test_sent','sent',
        'awaiting_reply','followup_sent','no_response',
        'failed','replied','opted_out'
    ));

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'gab_crm_mailing_followup_count_check'
    ) THEN
        ALTER TABLE gab_crm_mailing_recipients
            ADD CONSTRAINT gab_crm_mailing_followup_count_check
            CHECK (followup_count BETWEEN 0 AND 1);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS gab_crm_import_batches_created_idx
    ON gab_crm_import_batches(created_at DESC);

CREATE INDEX IF NOT EXISTS gab_crm_import_rows_batch_idx
    ON gab_crm_import_rows(batch_id);

CREATE INDEX IF NOT EXISTS gab_crm_import_rows_inn_idx
    ON gab_crm_import_rows(inn);

CREATE INDEX IF NOT EXISTS gab_crm_import_rows_email_idx
    ON gab_crm_import_rows(email);

CREATE UNIQUE INDEX IF NOT EXISTS gab_crm_mailing_unique_import_row
    ON gab_crm_mailing_recipients(mailing_id, import_row_id)
    WHERE import_row_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS gab_crm_mailing_followup_due_idx
    ON gab_crm_mailing_recipients(followup_due_at)
    WHERE status = 'awaiting_reply'
      AND followup_due_at IS NOT NULL;

COMMIT;

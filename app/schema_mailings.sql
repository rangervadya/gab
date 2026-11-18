CREATE TABLE IF NOT EXISTS gab_crm_mailings (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body_html TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','test_sent','ready','sending','sent','failed','archived')),
    test_sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gab_crm_mailing_attachments (
    id BIGSERIAL PRIMARY KEY,
    mailing_id BIGINT NOT NULL REFERENCES gab_crm_mailings(id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    storage_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gab_crm_mailing_recipients (
    id BIGSERIAL PRIMARY KEY,
    mailing_id BIGINT NOT NULL REFERENCES gab_crm_mailings(id) ON DELETE CASCADE,
    investor_id BIGINT REFERENCES gab_crm_investors(id) ON DELETE SET NULL,
    company_id BIGINT REFERENCES gab_crm_companies(id) ON DELETE SET NULL,
    contact_id BIGINT REFERENCES gab_crm_contacts(id) ON DELETE SET NULL,
    recipient_name TEXT NOT NULL DEFAULT '',
    email TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','no_email','test_sent','sent','failed','replied','opted_out')),
    error_message TEXT,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (mailing_id, investor_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS gab_crm_mailing_unique_email
ON gab_crm_mailing_recipients (mailing_id, lower(email))
WHERE email IS NOT NULL AND email <> '';

CREATE TABLE IF NOT EXISTS gab_crm_mailing_events (
    id BIGSERIAL PRIMARY KEY,
    mailing_id BIGINT NOT NULL REFERENCES gab_crm_mailings(id) ON DELETE CASCADE,
    recipient_id BIGINT REFERENCES gab_crm_mailing_recipients(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gab_crm_mailings_created_idx
ON gab_crm_mailings (created_at DESC);

CREATE INDEX IF NOT EXISTS gab_crm_mailing_attachments_mailing_idx
ON gab_crm_mailing_attachments (mailing_id);

CREATE INDEX IF NOT EXISTS gab_crm_mailing_recipients_mailing_idx
ON gab_crm_mailing_recipients (mailing_id);

CREATE INDEX IF NOT EXISTS gab_crm_mailing_events_mailing_idx
ON gab_crm_mailing_events (mailing_id, created_at DESC);

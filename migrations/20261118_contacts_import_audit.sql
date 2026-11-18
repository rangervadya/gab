BEGIN;

-- Multiple email addresses and phones for companies.
ALTER TABLE gab_crm_companies
    ADD COLUMN IF NOT EXISTS emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS phones JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Multiple email addresses and phones for individual contacts.
ALTER TABLE gab_crm_contacts
    ADD COLUMN IF NOT EXISTS emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS phones JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Preserve existing primary company contact details.
UPDATE gab_crm_companies
SET
    emails = CASE
        WHEN NULLIF(BTRIM(general_email), '') IS NOT NULL
        THEN jsonb_build_array(LOWER(BTRIM(general_email)))
        ELSE emails
    END,
    phones = CASE
        WHEN NULLIF(BTRIM(general_phone), '') IS NOT NULL
        THEN jsonb_build_array(BTRIM(general_phone))
        ELSE phones
    END
WHERE
    emails = '[]'::jsonb
    OR phones = '[]'::jsonb;

-- Preserve existing primary contact details.
UPDATE gab_crm_contacts
SET
    emails = CASE
        WHEN NULLIF(BTRIM(email), '') IS NOT NULL
        THEN jsonb_build_array(LOWER(BTRIM(email)))
        ELSE emails
    END,
    phones = CASE
        WHEN NULLIF(BTRIM(phone), '') IS NOT NULL
        THEN jsonb_build_array(BTRIM(phone))
        ELSE phones
    END
WHERE
    emails = '[]'::jsonb
    OR phones = '[]'::jsonb;

-- Ensure contact arrays always contain JSON arrays.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'gab_crm_companies_emails_array_check'
          AND conrelid = 'gab_crm_companies'::regclass
    ) THEN
        ALTER TABLE gab_crm_companies
        ADD CONSTRAINT gab_crm_companies_emails_array_check
        CHECK (jsonb_typeof(emails) = 'array') NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'gab_crm_companies_phones_array_check'
          AND conrelid = 'gab_crm_companies'::regclass
    ) THEN
        ALTER TABLE gab_crm_companies
        ADD CONSTRAINT gab_crm_companies_phones_array_check
        CHECK (jsonb_typeof(phones) = 'array') NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'gab_crm_contacts_emails_array_check'
          AND conrelid = 'gab_crm_contacts'::regclass
    ) THEN
        ALTER TABLE gab_crm_contacts
        ADD CONSTRAINT gab_crm_contacts_emails_array_check
        CHECK (jsonb_typeof(emails) = 'array') NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'gab_crm_contacts_phones_array_check'
          AND conrelid = 'gab_crm_contacts'::regclass
    ) THEN
        ALTER TABLE gab_crm_contacts
        ADD CONSTRAINT gab_crm_contacts_phones_array_check
        CHECK (jsonb_typeof(phones) = 'array') NOT VALID;
    END IF;
END
$$;

ALTER TABLE gab_crm_companies
    VALIDATE CONSTRAINT gab_crm_companies_emails_array_check;
ALTER TABLE gab_crm_companies
    VALIDATE CONSTRAINT gab_crm_companies_phones_array_check;
ALTER TABLE gab_crm_contacts
    VALIDATE CONSTRAINT gab_crm_contacts_emails_array_check;
ALTER TABLE gab_crm_contacts
    VALIDATE CONSTRAINT gab_crm_contacts_phones_array_check;

CREATE INDEX IF NOT EXISTS gab_crm_companies_emails_gin_idx
    ON gab_crm_companies USING GIN (emails);
CREATE INDEX IF NOT EXISTS gab_crm_companies_phones_gin_idx
    ON gab_crm_companies USING GIN (phones);
CREATE INDEX IF NOT EXISTS gab_crm_contacts_emails_gin_idx
    ON gab_crm_contacts USING GIN (emails);
CREATE INDEX IF NOT EXISTS gab_crm_contacts_phones_gin_idx
    ON gab_crm_contacts USING GIN (phones);

-- Import confirmation audit.
ALTER TABLE gab_crm_import_rows
    ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS gab_crm_import_events (
    id BIGSERIAL PRIMARY KEY,
    import_row_id BIGINT NOT NULL
        REFERENCES gab_crm_import_rows(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL
        CHECK (event_type IN (
            'confirmed_created',
            'confirmed_matched',
            'needs_review',
            'rejected'
        )),
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gab_crm_import_events_row_idx
    ON gab_crm_import_events(import_row_id, created_at DESC);

-- Allow one investor to have multiple recipient addresses in one campaign.
-- Keep the separate unique-email index to prevent sending to the same
-- email address more than once in one mailing.
ALTER TABLE gab_crm_mailing_recipients
    DROP CONSTRAINT IF EXISTS
    gab_crm_mailing_recipients_mailing_id_investor_id_key;

CREATE INDEX IF NOT EXISTS gab_crm_mailing_recipients_investor_idx
    ON gab_crm_mailing_recipients(mailing_id, investor_id);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON gab_crm_import_events TO gab_review_user;

GRANT USAGE, SELECT
    ON SEQUENCE gab_crm_import_events_id_seq TO gab_review_user;

COMMIT;

BEGIN;

CREATE TABLE IF NOT EXISTS gab_crm_import_enrichment (
    id BIGSERIAL PRIMARY KEY,
    import_row_id BIGINT NOT NULL
        REFERENCES gab_crm_import_rows(id) ON DELETE CASCADE,
    provider TEXT NOT NULL
        CHECK (provider IN ('dadata', 'checko')),
    entity_type TEXT NOT NULL
        CHECK (entity_type IN ('company', 'entrepreneur')),
    inn TEXT NOT NULL,
    ogrn TEXT,
    legal_name TEXT,
    status_code TEXT,
    status_name TEXT,
    region TEXT,
    legal_address TEXT,
    okved_code TEXT,
    okved_name TEXT,
    director_name TEXT,
    phones JSONB NOT NULL DEFAULT '[]'::jsonb,
    emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    website TEXT,
    revenue_year INTEGER,
    revenue_rub NUMERIC(18,2),
    match_status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (match_status IN ('candidate', 'confirmed', 'rejected', 'needs_review')),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (import_row_id, provider, inn)
);

CREATE INDEX IF NOT EXISTS gab_crm_import_enrichment_row_idx
    ON gab_crm_import_enrichment(import_row_id);

CREATE INDEX IF NOT EXISTS gab_crm_import_enrichment_inn_idx
    ON gab_crm_import_enrichment(inn);

CREATE INDEX IF NOT EXISTS gab_crm_import_enrichment_status_idx
    ON gab_crm_import_enrichment(match_status);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON gab_crm_import_enrichment TO gab_review_user;

GRANT USAGE, SELECT
    ON SEQUENCE gab_crm_import_enrichment_id_seq TO gab_review_user;

COMMIT;

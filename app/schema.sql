CREATE TABLE IF NOT EXISTS gab_review_records (
    record_id TEXT PRIMARY KEY,
    source_page INTEGER,
    source_row INTEGER,
    internal_name TEXT,
    counterparty_contacts TEXT,
    address TEXT,
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    review_status TEXT,
    review_reasons TEXT,
    additional_review_note TEXT,
    review_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_state IN ('pending','approved','corrected','needs_source_check','not_required')),
    reviewer_decision TEXT,
    reviewer_notes TEXT,
    corrected_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gab_review_records_review
    ON gab_review_records (review_required, review_state);

CREATE INDEX IF NOT EXISTS idx_gab_review_records_name
    ON gab_review_records (internal_name);

CREATE INDEX IF NOT EXISTS idx_gab_review_records_counterparty
    ON gab_review_records (counterparty_contacts);

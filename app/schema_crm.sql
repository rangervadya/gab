
CREATE TABLE IF NOT EXISTS gab_crm_companies (
    id BIGSERIAL PRIMARY KEY,
    company_name TEXT NOT NULL,
    inn TEXT,
    city TEXT,
    website TEXT,
    general_email TEXT,
    general_phone TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gab_crm_companies_inn
ON gab_crm_companies (inn)
WHERE inn IS NOT NULL AND btrim(inn) <> '';

CREATE TABLE IF NOT EXISTS gab_crm_contacts (
    id BIGSERIAL PRIMARY KEY,
    company_id BIGINT REFERENCES gab_crm_companies(id) ON DELETE SET NULL,
    full_name TEXT NOT NULL,
    position TEXT,
    phone TEXT,
    email TEXT,
    city TEXT,
    is_decision_maker BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gab_crm_contacts_company
ON gab_crm_contacts (company_id);

CREATE INDEX IF NOT EXISTS idx_gab_crm_contacts_email
ON gab_crm_contacts (lower(email));

CREATE TABLE IF NOT EXISTS gab_crm_assets (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    asset_type TEXT NOT NULL DEFAULT 'other'
        CHECK (asset_type IN (
            'shopping_center', 'commercial_space',
            'land', 'logistics', 'industrial', 'other'
        )),
    city TEXT,
    address TEXT,
    price_rub NUMERIC(18,2) CHECK (price_rub IS NULL OR price_rub >= 0),
    area_sqm NUMERIC(14,2) CHECK (area_sqm IS NULL OR area_sqm >= 0),
    annual_income_rub NUMERIC(18,2),
    expected_yield_pct NUMERIC(8,3),
    asset_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (asset_status IN ('draft','active','reserved','sold','archived')),
    description TEXT,
    key_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gab_crm_assets_status
ON gab_crm_assets (asset_status);

CREATE TABLE IF NOT EXISTS gab_crm_investors (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    company_id BIGINT REFERENCES gab_crm_companies(id) ON DELETE SET NULL,
    primary_contact_id BIGINT REFERENCES gab_crm_contacts(id) ON DELETE SET NULL,

    budget_min_rub NUMERIC(18,2),
    budget_max_rub NUMERIC(18,2),
    source_of_funds TEXT,

    preferred_asset_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    interested_asset_id BIGINT REFERENCES gab_crm_assets(id) ON DELETE SET NULL,

    investment_horizon TEXT NOT NULL DEFAULT 'unknown'
        CHECK (investment_horizon IN (
            'up_to_3_months', '3_to_6_months',
            '6_to_12_months', 'over_12_months', 'unknown'
        )),

    motivation TEXT,

    temperature TEXT NOT NULL DEFAULT 'cold'
        CHECK (temperature IN ('cold','warm','hot')),

    response_status TEXT NOT NULL DEFAULT 'not_contacted'
        CHECK (response_status IN (
            'not_contacted','awaiting_reply','replied',
            'no_response','unreachable'
        )),

    pipeline_stage TEXT NOT NULL DEFAULT 'new'
        CHECK (pipeline_stage IN (
            'new','email','call','conversation','qualification',
            'passed_to_director','negotiation','deal','refusal','nurture'
        )),

    has_budget_source BOOLEAN NOT NULL DEFAULT FALSE,
    interested_in_asset BOOLEAN NOT NULL DEFAULT FALSE,
    horizon_within_6_months BOOLEAN NOT NULL DEFAULT FALSE,
    decision_maker_access BOOLEAN NOT NULL DEFAULT FALSE,
    agreed_next_step BOOLEAN NOT NULL DEFAULT FALSE,

    qualification_score SMALLINT GENERATED ALWAYS AS (
        has_budget_source::int +
        interested_in_asset::int +
        horizon_within_6_months::int +
        decision_maker_access::int +
        agreed_next_step::int
    ) STORED,

    qualified BOOLEAN GENERATED ALWAYS AS (
        (
            has_budget_source::int +
            interested_in_asset::int +
            horizon_within_6_months::int +
            decision_maker_access::int +
            agreed_next_step::int
        ) >= 4
    ) STORED,

    lead_source TEXT,
    owner TEXT,
    next_step TEXT,
    next_step_at TIMESTAMPTZ,
    notes TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gab_crm_investors_temperature
ON gab_crm_investors (temperature);

CREATE INDEX IF NOT EXISTS idx_gab_crm_investors_stage
ON gab_crm_investors (pipeline_stage);

CREATE INDEX IF NOT EXISTS idx_gab_crm_investors_qualified
ON gab_crm_investors (qualified);

CREATE TABLE IF NOT EXISTS gab_crm_interactions (
    id BIGSERIAL PRIMARY KEY,
    investor_id BIGINT REFERENCES gab_crm_investors(id) ON DELETE SET NULL,
    company_id BIGINT REFERENCES gab_crm_companies(id) ON DELETE SET NULL,
    contact_id BIGINT REFERENCES gab_crm_contacts(id) ON DELETE SET NULL,
    asset_id BIGINT REFERENCES gab_crm_assets(id) ON DELETE SET NULL,

    interaction_type TEXT NOT NULL
        CHECK (interaction_type IN ('call','email','telegram','meeting','other')),

    direction TEXT NOT NULL DEFAULT 'outbound'
        CHECK (direction IN ('inbound','outbound')),

    subject TEXT,
    body TEXT,
    happened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gab_crm_interactions_investor
ON gab_crm_interactions (investor_id, happened_at DESC);

CREATE TABLE IF NOT EXISTS gab_crm_tasks (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    task_type TEXT NOT NULL DEFAULT 'other'
        CHECK (task_type IN ('call','email','meeting','follow_up','other')),

    due_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','done','cancelled')),

    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('low','normal','high')),

    assigned_to TEXT,
    investor_id BIGINT REFERENCES gab_crm_investors(id) ON DELETE SET NULL,
    company_id BIGINT REFERENCES gab_crm_companies(id) ON DELETE SET NULL,
    contact_id BIGINT REFERENCES gab_crm_contacts(id) ON DELETE SET NULL,
    asset_id BIGINT REFERENCES gab_crm_assets(id) ON DELETE SET NULL,

    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gab_crm_tasks_due
ON gab_crm_tasks (status, due_at);

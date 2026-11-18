CREATE TABLE IF NOT EXISTS gab_crm_notifications (
    id BIGSERIAL PRIMARY KEY,
    investor_id BIGINT NOT NULL
        REFERENCES gab_crm_investors(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL
        CHECK (event_type IN (
            'investor_became_hot',
            'investor_qualified'
        )),
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gab_crm_notifications_unread
ON gab_crm_notifications (read_at, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gab_crm_notifications_unique_unread
ON gab_crm_notifications (investor_id, event_type)
WHERE read_at IS NULL;

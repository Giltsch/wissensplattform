-- White-label branding configuration. Single-tenant deployment model: one
-- VPS = one customer. The CHECK on id enforces a singleton row so we never
-- accidentally end up with two competing brands and indeterminate UI.
--
-- Multi-tenancy (one install serving many customers) is a future
-- architectural change; when it lands, this table grows a tenant_id column
-- and the singleton CHECK is dropped.

CREATE TABLE IF NOT EXISTS branding (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    app_name TEXT NOT NULL DEFAULT 'stilbezirk',
    -- The bold word after app_name in the wordmark, e.g. "wissen".
    sub_brand TEXT NOT NULL DEFAULT 'wissen',
    -- 6- or 3-digit hex; validated again at the API layer.
    accent_color TEXT NOT NULL DEFAULT '#6DDD15',
    -- Short tagline shown on login and as a meta description; optional.
    tagline TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the singleton row. ON CONFLICT keeps re-runs idempotent.
INSERT INTO branding (id) VALUES (1)
    ON CONFLICT (id) DO NOTHING;

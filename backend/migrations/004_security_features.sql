-- Two new tables for the production security pass:
--   chat_usage  — per-user daily counter for /api/chat. A determined insider
--                 with a valid login can otherwise burn OpenRouter budget;
--                 SlowAPI's per-IP cap doesn't help because the IP rotates.
--   admin_audit — immutable record of every mutating admin action. Forensic
--                 trail for "who deleted that document?", supports breach
--                 review, and is a reasonable answer to GoBD-style audit
--                 requirements for a tool used in business workflows.

-- ---- Per-user daily chat counter -----------------------------------------
-- Composite PK lets us UPSERT cheaply: one row per user per day. Reset is
-- automatic — calls on a new day insert a new row at count=1.
CREATE TABLE IF NOT EXISTS chat_usage (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day DATE NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
-- Cleanup index for opportunistic pruning of rows older than a retention
-- window (operator decision; not run by the app).
CREATE INDEX IF NOT EXISTS chat_usage_day_idx ON chat_usage(day);

-- ---- Admin audit log -----------------------------------------------------
-- details is JSONB so we can attach action-specific payload without ALTERing
-- the table for each new field. Indexed by occurred_at desc for the typical
-- "show recent admin actions" query.
CREATE TABLE IF NOT EXISTS admin_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    action TEXT NOT NULL,           -- 'doc.upload', 'doc.delete', 'user.create', ...
    target_type TEXT NOT NULL,      -- 'document', 'user'
    target_id BIGINT,               -- nullable when the target is gone (e.g. delete after action)
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS admin_audit_occurred_idx ON admin_audit(occurred_at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_actor_idx ON admin_audit(actor_id, occurred_at DESC);
-- ON DELETE RESTRICT on actor_id means we can't delete a user who has audit
-- entries — by design. Pre-empt that lock by reassigning entries to a system
-- user before deletion if you ever need to.

-- Adds an admin role flag to users.
-- Idempotent so re-runs on an already-migrated DB are no-ops.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS users_is_admin_idx ON users(is_admin) WHERE is_admin;

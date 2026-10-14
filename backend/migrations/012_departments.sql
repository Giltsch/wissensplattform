-- Controlled vocabulary for the tag-based ACL. Replaces the free-text
-- comma-input with a managed list of departments. Existing
-- documents.tags / users.allowed_tags TEXT[] columns stay; what changes
-- is that admin endpoints now reject any tag value that's not in the
-- departments table.
--
-- Seeding: any tag currently in use somewhere is rolled into the
-- vocabulary so we don't break existing documents/users on next save.
-- Operators can then rename / delete entries from the admin UI.

CREATE TABLE IF NOT EXISTS departments (
    -- The slug used in documents.tags / users.allowed_tags arrays.
    -- Same grammar as acls.normalise_tag (lowercase ascii slug).
    name TEXT PRIMARY KEY,
    -- Display label shown in the UI. Same as name on seed; admins can
    -- edit it later (e.g. "hr" → "Personal & Recht").
    label TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by BIGINT REFERENCES users(id) ON DELETE SET NULL
);

-- One-time seed: every distinct tag currently appearing on a document
-- or in a user's allow-list becomes a department row. The label
-- defaults to the tag name; admins can rename in the UI. ON CONFLICT
-- DO NOTHING so re-applying the migration on a populated table is
-- idempotent (defence — apply_migrations already version-gates this).
INSERT INTO departments(name, label)
SELECT DISTINCT t, t FROM (
    SELECT unnest(tags) AS t FROM documents
    UNION
    SELECT unnest(allowed_tags) AS t FROM users
) sub
WHERE t IS NOT NULL
ON CONFLICT (name) DO NOTHING;

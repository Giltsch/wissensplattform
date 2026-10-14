-- Per-document role tags + per-user allow list. Tag-based ACLs that gate
-- which documents a non-admin user sees during retrieval and through the
-- public document endpoints.
--
-- Visibility rule (enforced server-side in rag.retrieve and main.py
-- list/get_document):
--   * is_admin = TRUE                       -> sees every document
--   * cardinality(documents.tags) = 0       -> public, every authed user sees it
--   * documents.tags && users.allowed_tags  -> visible (intersection non-empty)
--   * otherwise                             -> not visible
--
-- Defaulting both columns to '{}' keeps existing data behaving exactly as
-- before this migration: every existing document becomes "public" and
-- every existing user keeps seeing it. Operators opt in to the ACL by
-- tagging documents and granting tags on users.
--
-- Why TEXT[] not bitmask: Postgres-idiomatic, no fixed-role limit,
-- self-documenting (the row literally reads {hr,finance}), and pairs
-- with a GIN index on the array overlap operator (&&) for cheap filters.
-- The ICSA blueprint uses a 10-bit integer mask — fine for their fixed
-- vocabulary, but ours wants to be operator-extensible.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS allowed_tags TEXT[] NOT NULL DEFAULT '{}';

-- GIN supports the overlap (&&) operator we use for ACL filtering. Without
-- this index the filter would seq-scan documents on every retrieve call.
CREATE INDEX IF NOT EXISTS documents_tags_gin_idx
    ON documents USING gin (tags);

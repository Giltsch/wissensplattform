-- External source-type metadata + the fail-closed CRM landing department.
--
-- Phase 4 (CRM / external-DB connector). The CRM sync (backend/crm_sync.py)
-- batches anonymized records INTO the documents table — there is no live
-- query on the chat path. These columns record *what kind* of source a
-- document came from and a small structured metadata blob (connector name,
-- etc.); they are metadata only and do NOT participate in dedup (still keyed
-- on content_hash) or the ACL (still keyed on tags). DEFAULT 'file'/'{}'
-- backfills every existing row without disturbing either.
--
-- NOTE on 'origin' vs 'source_type': migration 015 added `origin`
-- ('local'/'url') as fetch-layer provenance for the URL/local connectors.
-- `source_type` is the coarser ingest-channel classification
-- ('file' | 'crm') used by the CRM pipeline. They are intentionally
-- separate dimensions: a CRM document has source_type='crm' and the CRM
-- sync leaves `origin` at its 'local' default (it is not an HTTP fetch).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'file';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_meta JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS documents_source_type_idx ON documents(source_type);

-- Fail-closed landing department for CRM records the operator-authored
-- mapping could not resolve (unknown/ambiguous crm_group). It is a real
-- department in the controlled vocabulary so assert_known_departments()
-- accepts it, but the operator must NEVER grant it to a user's
-- allowed_tags. Result: an unmapped CRM record is visible to admins only
-- (admin bypasses the visibility_clause) and to nobody else — strictly
-- safer than the public ('{}') case, which any authed user can read.
-- Granting it to a user is a deliberate operator action, not a default.
INSERT INTO departments(name, label)
    VALUES ('crm_unmapped', 'CRM (nicht zugeordnet)')
    ON CONFLICT (name) DO NOTHING;

-- Synthetic system actor for batch jobs that have no human admin behind
-- them (the CRM sync writes a per-run admin_audit row, and admin_audit.
-- actor_id is NOT NULL REFERENCES users(id) ON DELETE RESTRICT). This user
-- is is_active=FALSE so it can never log in (login.py rejects inactive
-- accounts before bcrypt), holds a non-verifiable password hash sentinel
-- (not a valid bcrypt hash, so bcrypt.checkpw can never match it), and is
-- NOT is_admin (it never authenticates, so the flag is moot). crm_sync.py
-- resolves this id by email; if it is missing the sync still proceeds but
-- skips the audit write (best-effort, like _audit) rather than crashing.
-- The fixed email is the stable lookup key.
INSERT INTO users(email, password_hash, is_active, is_admin)
    VALUES ('system+crm-sync@localhost.invalid', 'x-no-login-system-actor', FALSE, FALSE)
    ON CONFLICT (email) DO NOTHING;

-- db.apply_migrations() records this filename's stem in schema_migrations
-- after the file runs; the explicit insert keeps the file self-contained and
-- idempotent if applied out of band (matches the 001_init precedent).
INSERT INTO schema_migrations(version) VALUES ('016_external_sources')
    ON CONFLICT (version) DO NOTHING;

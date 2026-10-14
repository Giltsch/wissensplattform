-- Source provenance: record where each document came from so the admin UI
-- and audit log can distinguish a local file / upload from an externally
-- fetched URL. This is metadata only — it does NOT participate in dedup
-- (still keyed on content_hash) or the ACL (still keyed on tags), so the
-- DEFAULT 'local' backfills every existing row without disturbing either.
--
-- Phase 3 threads `origin` through ingest.ingest_item /
-- _insert_document_with_chunks ('local'/'file' for the filesystem walk and
-- admin upload, 'url' for the URL connector).

ALTER TABLE documents ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'local';

-- db.apply_migrations() records this filename's stem in schema_migrations
-- after the file runs; the explicit insert keeps the file self-contained and
-- idempotent if applied out of band (matches the 001_init precedent).
INSERT INTO schema_migrations(version) VALUES ('015_source_origin')
    ON CONFLICT (version) DO NOTHING;

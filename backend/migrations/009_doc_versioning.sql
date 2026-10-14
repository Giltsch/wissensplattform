-- Document versioning. Re-uploading a file with the same source name no
-- longer rejects with 409 — instead the previous current row is archived
-- (is_current = FALSE, superseded_at = now()) and a new row is inserted
-- with version = previous.version + 1 and parent_id pointing at it.
--
-- Retrieval filters by is_current = TRUE so only the latest content is
-- searchable. Old chunks remain in the DB for audit / future restore but
-- never appear in answers.
--
-- The content_hash uniqueness constraint stays — a re-upload of bytes
-- already in the DB (current OR archived) returns 409, since there's no
-- meaningful "version" of identical content.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS parent_id BIGINT REFERENCES documents(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;

-- Partial index: the hot path is "show me the current docs" / "search
-- only current chunks". Indexing only is_current=TRUE rows is cheaper
-- and the query planner picks it up for both.
CREATE INDEX IF NOT EXISTS documents_current_idx
    ON documents(source) WHERE is_current = TRUE;

-- For each source name, only one row may be current at a time. Without
-- this constraint a race between two simultaneous uploads could leave
-- two current versions, and retrieve() would return both (effectively
-- mixing versions in answers).
CREATE UNIQUE INDEX IF NOT EXISTS documents_one_current_per_source
    ON documents(source) WHERE is_current = TRUE;

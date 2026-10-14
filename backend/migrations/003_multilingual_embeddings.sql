-- Switch from English-only bge-small-en-v1.5 (384 dim) to a multilingual
-- embedding model (intfloat/multilingual-e5-base, 768 dim) so retrieval works
-- on the German document corpus this app actually serves.
--
-- Old 384-dim vectors are incompatible with the new model — they cannot be
-- "translated" — so we drop them. A re-ingest is required after this
-- migration runs:
--     docker compose exec backend python ingest.py /app/docs
--
-- The HNSW index is column-typed, so it must be dropped and recreated.

DROP INDEX IF EXISTS chunks_embedding_hnsw_idx;

-- TRUNCATE keeps the table itself, just wipes rows. RESTART IDENTITY resets
-- chunk ids; CASCADE not needed since nothing references chunks.id.
TRUNCATE TABLE chunks RESTART IDENTITY;

-- Documents stay in the table — but their content_hash uniqueness would
-- otherwise block re-ingest of the same files. Wipe documents too so the
-- next ingest run is a clean rebuild.
TRUNCATE TABLE documents RESTART IDENTITY CASCADE;

ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(768);

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

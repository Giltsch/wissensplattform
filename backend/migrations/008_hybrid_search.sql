-- Hybrid retrieval: keep the dense vector path, add a German-stemmed BM25
-- path on top. Dense embeddings are great for paraphrase and meaning but
-- weak on exact-token matches that don't appear in training (proper nouns,
-- product codes, internal identifiers like "WizExp"). tsvector + GIN gives
-- us cheap keyword recall to cover that gap.
--
-- 'german' is a built-in Postgres dictionary that handles stemming
-- (Mitarbeiter ≈ Mitarbeiterin) and stopword removal. For mixed German/
-- English corpora 'simple' would be more conservative — switch via a new
-- migration if recall on English terms suffers.
--
-- The column is GENERATED ALWAYS AS … STORED so it stays in sync with
-- content automatically; no application-level write path to remember.

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('german', content)) STORED;

-- GIN is the canonical index type for tsvector full-text search. The
-- write cost on ingest is small at our scale (a few hundred chunks per
-- document at most).
CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx
    ON chunks USING GIN(content_tsv);

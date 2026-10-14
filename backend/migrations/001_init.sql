-- Initial schema. Run once at startup by db.apply_migrations().
-- Idempotent via IF NOT EXISTS.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---- Migration tracking ---------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- Users ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    -- bcrypt hash; never the plaintext.
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Per-account lockout. IP-based throttling is handled separately by slowapi.
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ
);

-- ---- Sessions (server-side; not JWT) -------------------------------------
-- The cookie value is a random opaque token. We store its sha256 here so a
-- DB read does not leak live session ids — same idea as storing password
-- hashes rather than passwords.
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_idx    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);

-- ---- Login attempts (audit) ----------------------------------------------
-- Kept separate from users.failed_login_count so we can audit attempts
-- against unknown emails (otherwise we'd leak which emails exist).
CREATE TABLE IF NOT EXISTS login_attempts (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    ip TEXT,
    success BOOLEAN NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempts_email_time_idx
    ON login_attempts(email, occurred_at DESC);

-- ---- Documents ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    -- Hash of the *raw file bytes*. Re-ingesting the same file is a no-op.
    content_hash TEXT NOT NULL UNIQUE,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- Chunks ---------------------------------------------------------------
-- Embedding dim must match settings.embedding_dim (384 for bge-small-en-v1.5).
-- Swapping models means a new migration that drops + recreates this column.
CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    -- Per-chunk hash so we can dedupe at chunk granularity, not just file.
    content_hash TEXT NOT NULL,
    embedding vector(384) NOT NULL,
    UNIQUE (document_id, chunk_index),
    UNIQUE (content_hash)
);

-- HNSW gives better recall than IVFFlat at our scale and doesn't need a
-- training step. cosine distance op-class to match how rag.py queries.
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

INSERT INTO schema_migrations(version) VALUES ('001_init')
    ON CONFLICT (version) DO NOTHING;

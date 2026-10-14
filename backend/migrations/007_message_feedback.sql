-- 👍 / 👎 feedback per assistant message. One feedback per message — the
-- conversation owner is the only person allowed to rate, and they get one
-- "current opinion" per message. Updates UPSERT into the same row rather
-- than appending history.
--
-- Why message_id as PK (not (message_id, user_id)): a message belongs to
-- exactly one conversation, which belongs to exactly one user. The
-- API-layer ownership check makes the user_id column more for indexing
-- and forensics than for join semantics.

CREATE TABLE IF NOT EXISTS message_feedback (
    message_id BIGINT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- -1 = thumbs down, +1 = thumbs up. SMALLINT keeps the row tight; CHECK
    -- constraint mirrors the API-layer Pydantic Literal.
    rating SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
    -- Optional free-text — bounded by the API. Useful for understanding
    -- *why* a user gave a thumbs-down without forcing them to fill it in.
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Show me the worst-rated answers in the last N days" — this index makes
-- the analytics dashboard queries cheap.
CREATE INDEX IF NOT EXISTS message_feedback_rating_created_idx
    ON message_feedback(rating, created_at DESC);

-- "How many of THIS user's messages did they rate?" — secondary forensic /
-- analytics path.
CREATE INDEX IF NOT EXISTS message_feedback_user_idx
    ON message_feedback(user_id, created_at DESC);

-- Persistent chat history. Each user has many conversations; each
-- conversation holds an ordered sequence of user/assistant messages.
--
-- Privacy note: prior to this migration the app deliberately did NOT
-- persist message content (only metadata — see SECURITY.md A09). With
-- conversations enabled, content lives in the DB. The Datenschutz page
-- discloses this; operators who don't want this should disable the
-- conversation API and keep history client-side only.

CREATE TABLE IF NOT EXISTS conversations (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'Neue Unterhaltung',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Listing the user's conversations is the hot path: "show my recent chats
-- ordered by last activity". Composite index serves it without a sort.
CREATE INDEX IF NOT EXISTS conversations_user_updated_idx
    ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    -- CHECK constraint mirrors the Pydantic Literal on the wire — defence
    -- in depth so a future bug or direct SQL can't insert garbage roles.
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    -- For assistant turns: list of {source, chunk_index, text} as returned
    -- to the client. NULL on user turns (and on assistant errors).
    sources JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Loading a conversation's full transcript: PK on conversation_id + ordered
-- by created_at. id alone would also work since BIGSERIAL is monotonic, but
-- ordering by created_at is the explicit semantic.
CREATE INDEX IF NOT EXISTS messages_conversation_created_idx
    ON messages(conversation_id, created_at);

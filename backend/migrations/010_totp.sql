-- Optional TOTP (RFC 6238) second factor on the login flow. Two pieces:
--
--   1. users.totp_secret (base32) + totp_enabled. Secret is set during the
--      setup ritual but stays inert until verify confirms the user typed a
--      working code at least once — guards against "secret saved but app
--      never set up" lock-outs.
--
--   2. login_challenges — same shape as the sessions table. After the
--      password step succeeds for a TOTP-enabled user we issue an opaque
--      challenge token (cookie value: <random>.<hmac>, DB stores
--      sha256(<random>)). The client posts that token + the TOTP code to
--      /api/login/totp; only then does a real session cookie get set.
--      A challenge-table is preferable to a half-authenticated cookie
--      because losing the second step (browser close, network drop)
--      leaves no ambient state lying around to confuse the next visit.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS totp_secret TEXT,
    ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS login_challenges (
    id_hash TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS login_challenges_expires_idx
    ON login_challenges(expires_at);

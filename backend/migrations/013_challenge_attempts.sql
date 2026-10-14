-- Per-challenge attempt counter for the TOTP step of login.
--
-- Without this, a stolen challenge_token (from XSS, clipboard capture,
-- tab takeover, etc.) is valid for up to 5 minutes and can be retried
-- until the per-account lockout closes the door — that's enough time
-- to grind through quite a few 6-digit codes. Capping attempts per
-- challenge keeps the brute-force surface bounded by the per-challenge
-- counter even before the per-account counter trips (audit C1-HIGH-1,
-- 2026-05-12).
--
-- SMALLINT (16-bit) is plenty for a counter that triggers deletion at
-- MAX_CHALLENGE_ATTEMPTS (=5 today). NOT NULL DEFAULT 0 means existing
-- rows (none in production at migration time, but defensive) start
-- behaving like fresh challenges.

ALTER TABLE login_challenges
    ADD COLUMN IF NOT EXISTS attempts SMALLINT NOT NULL DEFAULT 0;

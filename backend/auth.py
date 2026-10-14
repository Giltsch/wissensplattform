"""Authentication: passwords, sessions, lockout, add-user CLI.

Sessions are server-side. We generate 32 bytes of random, set that as the
cookie value (httpOnly, Secure, SameSite=Strict), and store its sha256 in
Postgres. A leaked sessions table doesn't yield live cookies; an attacker
would need both the DB read and a way to derive the original token, which
sha256 doesn't allow.

Lockout policy: 10 failed attempts within the lockout window puts the
account into a 15-minute timeout. The slowapi rate limiter on the route
provides per-IP throttling on top of this — this lockout is per-account.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import hmac
import logging
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Cookie, HTTPException, Request, status

from config import get_settings
from db import apply_migrations, close_pool, connection, init_pool

log = logging.getLogger(__name__)

# Cookie name. When `cookie_secure=True` (production behind TLS) we use
# the `__Host-` prefix — browsers refuse to accept a `__Host-` cookie
# without `Secure`, `Path=/`, and no `Domain`, all three of which we
# already do. The prefix makes the cookie inviolable from subdomain
# overrides — even a sibling `evil.support.example.com` (e.g. via a
# misconfigured Caddy on:host) cannot set a same-named cookie on the
# parent domain (audit C2-LOW-7, 2026-05-12). In dev (`cookie_secure
# =False`) we fall back to the plain name so plain-HTTP localhost
# still works.
_SESSION_COOKIE_NAME_HOSTPREFIX = "__Host-rag_session"
_SESSION_COOKIE_NAME_DEV = "rag_session"


def _cookie_name() -> str:
    return (
        _SESSION_COOKIE_NAME_HOSTPREFIX
        if get_settings().cookie_secure
        else _SESSION_COOKIE_NAME_DEV
    )


# Backwards-compatibility constant for callers that imported the old
# name. Resolves at import time, which is fine because Settings are
# cached and never change after startup.
SESSION_COOKIE_NAME = _cookie_name()
# Session token format: <id_hex>.<hmac_hex>. The DB key is sha256(<id_hex>).
# The HMAC binds the cookie to SESSION_SECRET so an attacker cannot mint a
# valid-looking cookie even if they know a session id from a leaked DB.
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}\.[0-9a-f]{64}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


# ---- Password hashing ----------------------------------------------------

# bcrypt silently truncates inputs to 72 bytes — two passwords that
# match in the first 72 bytes would otherwise validate interchangeably
# (audit C3-LOW-8, 2026-05-12). The Pydantic LoginRequest cap is 256
# *chars*; UTF-8 multi-byte glyphs can push that well past 72 bytes.
# We reject here so the error is loud, not silent.
_BCRYPT_MAX_BYTES = 72


def hash_password(plaintext: str) -> str:
    encoded = plaintext.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_BYTES:
        raise ValueError(
            "password exceeds 72-byte bcrypt limit after UTF-8 encoding"
        )
    settings = get_settings()
    return bcrypt.hashpw(
        encoded, bcrypt.gensalt(rounds=settings.bcrypt_cost)
    ).decode("ascii")


def verify_password(plaintext: str, hashed: str) -> bool:
    encoded = plaintext.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_BYTES:
        # Long inputs would have been truncated at hash time; rather
        # than risk a false-match against an honest 72-byte hash, fail
        # closed. The user-facing message stays generic upstream.
        return False
    try:
        return bcrypt.checkpw(encoded, hashed.encode("ascii"))
    except ValueError:
        # Malformed hash in the DB. Treat as failure rather than crashing.
        return False


# Lazily-generated, cached dummy hash for the unknown-email branch of
# `login()`. Cost matches whatever `bcrypt_cost` is at first call —
# regenerate by clearing the cache and re-importing if you bump cost.
_DUMMY_HASH_CACHE: Optional[str] = None


def _dummy_hash() -> str:
    global _DUMMY_HASH_CACHE
    if _DUMMY_HASH_CACHE is None:
        settings = get_settings()
        _DUMMY_HASH_CACHE = bcrypt.hashpw(
            b"dummy-not-a-real-password",
            bcrypt.gensalt(rounds=settings.bcrypt_cost),
        ).decode("ascii")
    return _DUMMY_HASH_CACHE


# ---- Token helpers -------------------------------------------------------

def _hmac_hex(message: str) -> str:
    secret = get_settings().session_secret.get_secret_value().encode("utf-8")
    return hmac.new(secret, message.encode("ascii"), hashlib.sha256).hexdigest()


def _new_token() -> tuple[str, str]:
    """Return (cookie_value, db_key) for a freshly-minted session."""
    raw_id = secrets.token_hex(32)
    cookie = f"{raw_id}.{_hmac_hex(raw_id)}"
    db_key = hashlib.sha256(raw_id.encode("ascii")).hexdigest()
    return cookie, db_key


def _verify_token(cookie: str) -> Optional[str]:
    """Return the DB key if the cookie is well-formed and signature is valid."""
    if not _TOKEN_RE.match(cookie):
        return None
    raw_id, sig = cookie.split(".", 1)
    expected = _hmac_hex(raw_id)
    if not hmac.compare_digest(sig, expected):
        return None
    return hashlib.sha256(raw_id.encode("ascii")).hexdigest()


# ---- Lockout / login ----------------------------------------------------

class LoginResult:
    OK = "ok"
    BAD = "bad"
    LOCKED = "locked"
    INACTIVE = "inactive"
    # Password verified, account fine, but the user has 2FA enabled.
    # The route handler must respond with the challenge token instead
    # of a session cookie and direct the client to POST /api/login/totp.
    TOTP_REQUIRED = "totp_required"


# Login challenge tokens reuse the session token format (<id>.<hmac>) but
# live in a separate table with a 5-minute TTL and single-use semantics.
CHALLENGE_TTL_MINUTES = 5
# Per-challenge attempt cap. A stolen challenge_token would otherwise be
# valid for the full TTL, long enough to grind through several thousand
# 6-digit codes. Five is plenty for a real user fat-fingering the code
# and small enough that brute-force is hopeless. The per-account lockout
# (max_failed_logins) is the broader gate; this is defense in depth.
MAX_CHALLENGE_ATTEMPTS = 5


def _new_challenge_token() -> tuple[str, str]:
    raw_id = secrets.token_hex(32)
    cookie = f"{raw_id}.{_hmac_hex(raw_id)}"
    db_key = hashlib.sha256(raw_id.encode("ascii")).hexdigest()
    return cookie, db_key


def _verify_challenge_token(value: str) -> Optional[str]:
    """Validate signature and return DB key — same protocol as session
    cookies. Caller still has to consult login_challenges to confirm the
    token is unspent and unexpired."""
    return _verify_token(value)


async def _record_attempt(email: str, ip: Optional[str], success: bool) -> None:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO login_attempts(email, ip, success) VALUES (%s, %s, %s)",
                (email, ip, success),
            )
        await conn.commit()


async def _bump_failed(user_id: int) -> None:
    settings = get_settings()
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET failed_login_count = failed_login_count + 1, "
                "locked_until = CASE "
                "  WHEN failed_login_count + 1 >= %s "
                "  THEN now() + (%s || ' minutes')::interval "
                "  ELSE locked_until END "
                "WHERE id = %s",
                (settings.max_failed_logins, str(settings.lockout_minutes), user_id),
            )
        await conn.commit()


async def _reset_failed(user_id: int) -> None:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET failed_login_count = 0, locked_until = NULL "
                "WHERE id = %s",
                (user_id,),
            )
        await conn.commit()


async def login(email: str, password: str, ip: Optional[str]) -> tuple[str, Optional[str]]:
    """Attempt login. Returns (status, cookie_value_or_None)."""
    email = email.strip().lower()
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, password_hash, is_active, locked_until "
                "FROM users WHERE email = %s",
                (email,),
            )
            row = await cur.fetchone()

    # Always run bcrypt to keep timing roughly constant whether the email
    # exists or not. The dummy hash is generated lazily at the
    # configured cost so timing stays matched even if bcrypt_cost is
    # bumped later (audit C1-INFO-9, 2026-05-12) — a hardcoded
    # cost-12 hash would diverge in latency once an operator raises
    # the cost to 14 and quietly re-open the user-enumeration timing
    # side channel.
    DUMMY = _dummy_hash()
    valid = False
    user_id: Optional[int] = None
    is_active = False
    locked: Optional[datetime] = None

    if row is not None:
        user_id = row["id"]
        is_active = bool(row["is_active"])
        locked = row["locked_until"]
        valid = verify_password(password, row["password_hash"])
    else:
        verify_password(password, DUMMY)  # discard result, equalize timing

    now = datetime.now(timezone.utc)
    if row is None or not valid:
        await _record_attempt(email, ip, success=False)
        if user_id is not None:
            await _bump_failed(user_id)
        return LoginResult.BAD, None

    if not is_active:
        await _record_attempt(email, ip, success=False)
        return LoginResult.INACTIVE, None

    if locked is not None and locked > now:
        await _record_attempt(email, ip, success=False)
        return LoginResult.LOCKED, None

    # Password is correct. If the user has 2FA enabled, the password
    # step alone is not enough — issue a single-use, short-lived
    # challenge token instead of a session cookie. The /api/login/totp
    # endpoint converts the challenge plus a valid code into a real
    # session.
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT totp_enabled FROM users WHERE id = %s",
                (user_id,),
            )
            tr = await cur.fetchone()
    totp_enabled = bool(tr and tr["totp_enabled"])

    if totp_enabled:
        challenge, ch_key = _new_challenge_token()
        expires_at = now + timedelta(minutes=CHALLENGE_TTL_MINUTES)
        async with connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO login_challenges(id_hash, user_id, expires_at) "
                    "VALUES (%s, %s, %s)",
                    (ch_key, user_id, expires_at),
                )
            await conn.commit()
        # Don't reset failed_login_count yet — only the TOTP step closes
        # the loop. A bad TOTP attempt should still count toward lockout
        # to prevent code-guessing once an attacker knows the password.
        # We DO record the password-step attempt as success so audit
        # logs reflect that the password was correct.
        await _record_attempt(email, ip, success=True)
        return LoginResult.TOTP_REQUIRED, challenge

    # Success path (no 2FA).
    await _reset_failed(user_id)  # type: ignore[arg-type]
    await _record_attempt(email, ip, success=True)

    cookie, db_key = _new_token()
    settings = get_settings()
    expires_at = now + timedelta(hours=settings.session_ttl_hours)
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO sessions(id_hash, user_id, expires_at) "
                "VALUES (%s, %s, %s)",
                (db_key, user_id, expires_at),
            )
        await conn.commit()
    return LoginResult.OK, cookie


async def login_with_totp(
    challenge_token: str, code: str, ip: Optional[str],
) -> tuple[str, Optional[str]]:
    """Second login step. Validate (challenge, code) and on success mint
    a real session cookie. Returns (LoginResult, cookie_or_None) — same
    convention as login()."""
    from totp import verify_code  # avoid import cycle at module load

    ch_key = _verify_challenge_token(challenge_token)
    if ch_key is None:
        return LoginResult.BAD, None

    now = datetime.now(timezone.utc)
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id, expires_at FROM login_challenges WHERE id_hash = %s",
                (ch_key,),
            )
            row = await cur.fetchone()
    if row is None or row["expires_at"] <= now:
        # Treat expired/missing the same — never reveal which.
        return LoginResult.BAD, None

    user_id = row["user_id"]
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT email, is_active, locked_until, totp_secret, totp_enabled "
                "FROM users WHERE id = %s",
                (user_id,),
            )
            user = await cur.fetchone()
    if (user is None or not user["is_active"]
            or not user["totp_enabled"] or not user["totp_secret"]):
        return LoginResult.BAD, None
    if user["locked_until"] is not None and user["locked_until"] > now:
        return LoginResult.LOCKED, None

    from secret_box import unwrap as _unwrap_secret
    secret_plain = _unwrap_secret(user["totp_secret"])
    if not verify_code(secret_plain, code):
        # Bump per-account lockout AND per-challenge counter. If the
        # challenge's own attempt count hits MAX_CHALLENGE_ATTEMPTS,
        # invalidate it right here so a stolen challenge_token can't
        # be ground through code-by-code until expiry. The per-account
        # counter is the broader gate; this is defense in depth.
        await _bump_failed(user_id)
        await _record_attempt(user["email"], ip, success=False)
        async with connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE login_challenges SET attempts = attempts + 1 "
                    "WHERE id_hash = %s RETURNING attempts",
                    (ch_key,),
                )
                arow = await cur.fetchone()
                if arow is not None and arow["attempts"] >= MAX_CHALLENGE_ATTEMPTS:
                    await cur.execute(
                        "DELETE FROM login_challenges WHERE id_hash = %s",
                        (ch_key,),
                    )
            await conn.commit()
        return LoginResult.BAD, None

    # Both factors complete — consume the challenge and mint a session.
    settings = get_settings()
    cookie, db_key = _new_token()
    expires_at = now + timedelta(hours=settings.session_ttl_hours)
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM login_challenges WHERE id_hash = %s",
                (ch_key,),
            )
            await cur.execute(
                "INSERT INTO sessions(id_hash, user_id, expires_at) "
                "VALUES (%s, %s, %s)",
                (db_key, user_id, expires_at),
            )
        await conn.commit()
    await _reset_failed(user_id)
    return LoginResult.OK, cookie


async def logout(cookie_value: str) -> None:
    db_key = _verify_token(cookie_value)
    if db_key is None:
        return
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM sessions WHERE id_hash = %s", (db_key,))
        await conn.commit()


async def current_user(cookie_value: Optional[str]) -> Optional[dict]:
    """Resolve a session cookie to a user record, or None if unauthenticated."""
    if cookie_value is None:
        return None
    db_key = _verify_token(cookie_value)
    if db_key is None:
        return None
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT u.id, u.email, u.is_admin, u.allowed_tags, s.expires_at "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.id_hash = %s AND u.is_active = TRUE",
                (db_key,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    if row["expires_at"] <= datetime.now(timezone.utc):
        # Expired — clean up opportunistically.
        async with connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM sessions WHERE id_hash = %s", (db_key,))
            await conn.commit()
        return None
    # Touch last_seen_at without blocking the response (best-effort).
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE sessions SET last_seen_at = now() WHERE id_hash = %s",
                (db_key,),
            )
        await conn.commit()
    return {
        "id": row["id"],
        "email": row["email"],
        "is_admin": bool(row["is_admin"]),
        # ACL filter input for retrieve() and the public document endpoints.
        # Stored as TEXT[] in users.allowed_tags (see migration 011).
        "allowed_tags": list(row["allowed_tags"] or []),
    }


# ---- FastAPI dependency --------------------------------------------------

async def require_user(
    request: Request,
    rag_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict:
    user = await current_user(rag_session)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth required")
    # Stash on request.state so handlers can log user_id without re-fetching.
    request.state.user_id = user["id"]
    return user


async def require_admin(
    request: Request,
    rag_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict:
    """Like require_user but additionally rejects non-admins with 403."""
    user = await current_user(rag_session)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth required")
    if not user.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    request.state.user_id = user["id"]
    return user


# ---- CLI: add-user -------------------------------------------------------

async def _add_user_cli(email: str, password: Optional[str], admin: bool) -> int:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        print(f"invalid email: {email}", file=sys.stderr)
        return 2
    if password is None:
        password = getpass.getpass("password: ")
        confirm = getpass.getpass("confirm:  ")
        if password != confirm:
            print("passwords do not match", file=sys.stderr)
            return 2
    if len(password) < 12:
        print("password must be at least 12 characters", file=sys.stderr)
        return 2

    pw_hash = hash_password(password)
    await apply_migrations()
    await init_pool()
    try:
        async with connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO users(email, password_hash, is_admin) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (email) DO UPDATE "
                    "SET password_hash = EXCLUDED.password_hash, "
                    "    is_admin = EXCLUDED.is_admin OR users.is_admin, "
                    "    failed_login_count = 0, locked_until = NULL "
                    "RETURNING id, is_admin",
                    (email, pw_hash, admin),
                )
                row = await cur.fetchone()
            await conn.commit()
        print(f"ok: user id={row['id']} email={email} admin={row['is_admin']}")
    finally:
        await close_pool()
    return 0


def main() -> int:
    logging.basicConfig(level=get_settings().log_level)
    parser = argparse.ArgumentParser(prog="backend.auth")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-user", help="Create or reset a user")
    add.add_argument("email")
    # --password is optional; prompt if omitted. Avoids password-in-shell-history.
    add.add_argument("--password", default=None, help="If omitted, prompt interactively")
    add.add_argument("--admin", action="store_true", help="Grant admin role")

    args = parser.parse_args()
    if args.cmd == "add-user":
        return asyncio.run(_add_user_cli(args.email, args.password, args.admin))
    return 1


if __name__ == "__main__":
    sys.exit(main())

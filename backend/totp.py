"""TOTP (RFC 6238) helpers and self-service 2FA endpoints.

Mounted under /api/me/2fa. Each endpoint is `require_user` — a user can
only act on their own 2FA state. Admins enable/disable other users'
accounts indirectly via reset-password (which clears the TOTP secret as
a side effect — see admin.py).

Why TOTP and not WebAuthn / Passkeys: TOTP works with any authenticator
app the user already has (Aegis, 1Password, Bitwarden, Google
Authenticator) and needs zero browser-side cryptography support. The
trade-off is that the secret lives in the DB; we mitigate by treating
it like a password (never logged) and rotating it on disable+re-enable.

NOTE: no ``from __future__ import annotations`` here. slowapi's
``@limiter.limit`` wraps routes in a function whose ``__globals__``
don't see our local Pydantic body models, so FastAPI cannot resolve
string ForwardRefs and reclassifies the body as a query param. Keep
concrete annotations and explicit ``Body(...)`` defaults on
rate-limited routes (the same gotcha bites main.py).
"""

import base64
import io
import logging
from typing import Optional

import pyotp
import qrcode
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from auth import _bump_failed, require_user
from db import connection
from rate_limit import limiter
from secret_box import is_wrapped, unwrap, wrap

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/me/2fa", tags=["2fa"])


# ---- Models --------------------------------------------------------------

class TwoFAStatus(BaseModel):
    enabled: bool


class SetupResponse(BaseModel):
    """Returned from setup. The secret + provisioning URI are shown to the
    user once. After verify the secret stays in the DB but never leaves
    the server again."""
    secret: str
    otpauth_uri: str
    qr_png_data_uri: str


class CodeRequest(BaseModel):
    # 6-digit RFC 6238 codes; some authenticators support 8 — leave room.
    # Numeric-only allowlist rejects non-digit garbage before pyotp
    # parses it (audit C3-LOW-7, 2026-05-12). pyotp tolerates spaces
    # and dashes via .strip() but never returns true for non-digit
    # input; the allowlist makes the API contract explicit.
    code: str = Field(min_length=6, max_length=10, pattern=r"^\d{6,10}$")


# ---- Helpers -------------------------------------------------------------

async def _otpauth_uri(email: str, secret: str) -> str:
    # The QR issuer is the white-label brand name so the authenticator app
    # shows the operator's name, not a hardcoded literal. Cheap singleton
    # read; if branding is missing or empty we fall back to a neutral name.
    # Deliberately decoupled from openrouter_title (an LLM-attribution
    # string, not a brand).
    issuer = "Wissensplattform"
    try:
        async with connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT app_name, sub_brand FROM branding WHERE id = 1"
                )
                row = await cur.fetchone()
        if row and row["app_name"]:
            issuer = row["app_name"]
            if row["sub_brand"]:
                issuer = f"{row['app_name']} {row['sub_brand']}"
    except Exception:
        # A branding read failure must not block 2FA setup — keep the
        # neutral fallback.
        log.warning("branding read for otpauth issuer failed; using fallback")
    # pyotp's provisioning_uri is well-tested and handles URL-encoding.
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def _qr_png_data_uri(uri: str) -> str:
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _verify_code(secret: str, code: str) -> bool:
    """Verify a code against a base32 secret with ±1 step (30 s) tolerance.
    Tolerance window covers small clock drift between server and the user's
    phone — RFC 6238 explicitly allows up to one step on each side."""
    try:
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
    except Exception:
        # Malformed code or secret — treat as failed verification, never
        # raise (the API surface translates that to a generic 400).
        return False


# ---- Endpoints -----------------------------------------------------------

@router.get("", response_model=TwoFAStatus)
async def status_endpoint(user: dict = Depends(require_user)) -> TwoFAStatus:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT totp_enabled FROM users WHERE id = %s",
                (user["id"],),
            )
            row = await cur.fetchone()
    return TwoFAStatus(enabled=bool(row and row["totp_enabled"]))


@router.post("/setup", response_model=SetupResponse)
@limiter.limit("5/minute")
async def setup(
    request: Request,
    user: dict = Depends(require_user),
) -> SetupResponse:
    """Begin 2FA setup. Refuses if the account already has 2FA enabled —
    rotation goes through disable→setup so a hijacked session cannot
    overwrite a live secret (audit C-1, 2026-05-10).

    Rate-limited (5/min) so an attacker with a stolen session can't
    spam-rotate the pending secret and burn CPU rendering QR PNGs
    (audit C2-MED-3, 2026-05-12).

    The pre-read + UPDATE pair is collapsed into a single atomic
    `UPDATE ... WHERE totp_enabled = FALSE RETURNING` so two parallel
    setup calls can't race past the enabled-check (audit C3-MED-3).
    """
    secret = pyotp.random_base32()  # 32-char base32 = 160 bits, RFC-recommended
    # Atomic check-and-set: only writes when totp_enabled is still FALSE.
    # The RETURNING tells us whether the row matched. Wrap before
    # storing so a DB or backup leak doesn't expose the seed.
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET totp_secret = %s "
                "WHERE id = %s AND totp_enabled = FALSE "
                "RETURNING 1",
                (wrap(secret), user["id"]),
            )
            updated = await cur.fetchone()
        await conn.commit()
    if updated is None:
        # Either the user doesn't exist (impossible — require_user just
        # resolved them) or totp_enabled is already TRUE. Same 409 as
        # before: refuses to overwrite a live secret from a hijacked
        # session.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="2fa already enabled; disable it first to rotate",
        )
    uri = await _otpauth_uri(user["email"], secret)
    return SetupResponse(
        secret=secret,
        otpauth_uri=uri,
        qr_png_data_uri=_qr_png_data_uri(uri),
    )


@router.post("/verify")
@limiter.limit("5/minute")
async def verify(
    request: Request,
    body: CodeRequest = Body(...),
    user: dict = Depends(require_user),
) -> dict:
    """Verify a code against the stored (candidate) secret. On success
    sets totp_enabled = TRUE. Until verify the user can still log in
    with password alone — designed to prevent self-lockout if they
    can't reach their authenticator after setup.

    Rate-limited per IP and bumps the per-account lockout counter on
    bad codes so brute-force from an authed session lands on the same
    cliff as login brute-force (audit H-3, 2026-05-10)."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT totp_secret FROM users WHERE id = %s",
                (user["id"],),
            )
            row = await cur.fetchone()
    if not row or not row["totp_secret"]:
        # No setup in progress.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2fa setup not started",
        )
    stored = row["totp_secret"]
    secret = unwrap(stored)
    if not _verify_code(secret, body.code):
        # Per-account lockout shares the failed_login_count column with the
        # login flow — 10 fails in any combination across login/verify/disable
        # locks the account for 15 min. Generic error so a wrong-code response
        # doesn't reveal whether setup was in progress.
        await _bump_failed(user["id"])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid code",
        )
    async with connection() as conn:
        async with conn.cursor() as cur:
            # If this was a legacy plaintext row and a key is now
            # configured, rewrap on the spot — lazy migration off
            # plaintext (audit C6-HIGH-6, 2026-05-12).
            if not is_wrapped(stored):
                await cur.execute(
                    "UPDATE users SET totp_secret = %s, totp_enabled = TRUE "
                    "WHERE id = %s",
                    (wrap(secret), user["id"]),
                )
            else:
                await cur.execute(
                    "UPDATE users SET totp_enabled = TRUE WHERE id = %s",
                    (user["id"],),
                )
        await conn.commit()
    log.info("2fa_verified user_id=%s", user["id"])
    return {"ok": True}


@router.post("/disable")
@limiter.limit("5/minute")
async def disable(
    request: Request,
    body: CodeRequest = Body(...),
    user: dict = Depends(require_user),
) -> dict:
    """Disable 2FA. Requires a current valid code so that a hijacked
    session can't disable 2FA without access to the authenticator. To
    recover from a lost device, an admin must call reset-password
    (which clears the secret as a side effect).

    Rate-limited per IP + per-account lockout on bad codes (audit H-3)."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT totp_secret, totp_enabled FROM users WHERE id = %s",
                (user["id"],),
            )
            row = await cur.fetchone()
    if not row or not row["totp_enabled"] or not row["totp_secret"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2fa not enabled",
        )
    if not _verify_code(unwrap(row["totp_secret"]), body.code):
        await _bump_failed(user["id"])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid code",
        )
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET totp_secret = NULL, totp_enabled = FALSE "
                "WHERE id = %s",
                (user["id"],),
            )
        await conn.commit()
    log.info("2fa_disabled user_id=%s", user["id"])
    return {"ok": True}


# ---- Internal helpers used by the login flow ----------------------------

async def get_totp_state(user_id: int) -> tuple[bool, Optional[str]]:
    """Returns (enabled, secret_or_None). secret is the *active* secret
    — only meaningful when enabled. Used by the password-step in
    auth.login to decide whether to issue a session cookie or a
    challenge token instead."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT totp_secret, totp_enabled FROM users WHERE id = %s",
                (user_id,),
            )
            row = await cur.fetchone()
    if not row:
        return False, None
    return bool(row["totp_enabled"]), row["totp_secret"] if row["totp_enabled"] else None


def verify_code(secret: str, code: str) -> bool:
    """Public wrapper for the login flow."""
    return _verify_code(secret, code)

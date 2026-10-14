"""AEAD wrapper for at-rest secrets (TOTP seeds today; extensible later).

Audit C6-HIGH-6, 2026-05-12: TOTP secrets were stored as base32 plaintext
in `users.totp_secret`. A DB leak (or unencrypted backup) instantly
compromised every second factor. This module wraps them in Fernet
(AES-128-CBC + HMAC-SHA-256) with a key from `TOTP_KMS_KEY` that lives
in `.env`, separate from the DB.

Backward compatibility: existing plaintext rows must keep working until
the operator runs a rewrap pass. `unwrap()` therefore tries Fernet
decrypt first and, on `InvalidToken`, returns the input unchanged
(treating it as legacy plaintext). New writes always go through
`wrap()`, which encrypts if the key is configured and falls back to
plaintext with a loud WARNING log otherwise (so missing-key
deployments are visible in the logs every TOTP setup).

The base32 secret pyotp emits has no characters that overlap with
Fernet's base64-urlsafe output beyond the alphabet; Fernet tokens
always start with `gAAAAA` (version-0x80 byte) so detection is
unambiguous in practice — we just rely on `decrypt()` itself.
"""
from __future__ import annotations

import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from config import get_settings

log = logging.getLogger(__name__)

_warned_no_key = False


def _fernet() -> Optional[Fernet]:
    """Return a Fernet instance if a key is configured, else None.

    Logs a one-shot WARNING the first time it's called without a key, so
    a misconfigured deployment isn't silent — but doesn't spam logs on
    every write.
    """
    global _warned_no_key
    raw = get_settings().totp_kms_key.get_secret_value()
    if not raw:
        if not _warned_no_key:
            log.warning(
                "TOTP_KMS_KEY not set — TOTP secrets will be stored in "
                "plaintext (pre-audit posture). Generate a key and set "
                "it in .env to enable at-rest encryption."
            )
            _warned_no_key = True
        return None
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError):
        # Misshaped key (wrong length, not base64). Don't crash the
        # service — fall back to plaintext-write and log loud. The
        # operator can correct .env without redeploying.
        log.error(
            "TOTP_KMS_KEY is malformed (must be url-safe base64 of "
            "exactly 32 bytes). Falling back to plaintext writes."
        )
        return None


def wrap(plaintext: str) -> str:
    """Encrypt and return a Fernet token, or return plaintext unchanged
    if no key is configured. Called on every write to `totp_secret`."""
    f = _fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def unwrap(stored: str) -> str:
    """Decrypt a stored secret. Returns the input unchanged if it isn't
    a Fernet token (i.e. it's a legacy plaintext row from before the
    encryption migration). Never raises — a malformed row simply means
    "treat as plaintext and let the TOTP verify fail downstream".
    """
    f = _fernet()
    if f is None:
        # No key — every stored row is plaintext.
        return stored
    try:
        return f.decrypt(stored.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Legacy plaintext (pre-migration). Caller will rewrap on the
        # next successful verify.
        return stored


def is_wrapped(stored: str) -> bool:
    """True iff `stored` is currently a Fernet token (so we know whether
    a rewrap pass needs to touch it). Cheap version check — Fernet
    tokens always start with the version byte 0x80, which base64-urlsafe
    encodes to `gAAAAA`."""
    return stored.startswith("gAAAAA")

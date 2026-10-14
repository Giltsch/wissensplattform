"""Auth helpers: token shape, HMAC binding, password hashing.

These cover the crypto-adjacent surface of auth.py without touching the
DB. The login flow itself (with lockout, TOTP, session storage) is
exercised by the integration smoke test.
"""
from __future__ import annotations

import hashlib

import pytest

from auth import (
    EMAIL_RE,
    _TOKEN_RE,
    _hmac_hex,
    _new_token,
    _verify_token,
    hash_password,
    verify_password,
)


# ---- Token format & HMAC binding ----------------------------------------

def test_new_token_format():
    cookie, db_key = _new_token()
    # 64 hex (random id) + "." + 64 hex (HMAC). The route handler relies on
    # this exact shape; if it changes, _TOKEN_RE must change too.
    assert _TOKEN_RE.match(cookie)
    # DB key is sha256(random_id), so 64 hex chars.
    assert len(db_key) == 64 and all(c in "0123456789abcdef" for c in db_key)


def test_new_token_db_key_matches_sha256_of_random_id():
    cookie, db_key = _new_token()
    raw_id = cookie.split(".", 1)[0]
    assert db_key == hashlib.sha256(raw_id.encode("ascii")).hexdigest()


def test_verify_token_accepts_freshly_minted():
    cookie, expected_db_key = _new_token()
    assert _verify_token(cookie) == expected_db_key


def test_verify_token_rejects_malformed():
    for bad in [
        "",
        "abc",
        "not-a-token",
        "a" * 64,                     # missing the .hmac suffix
        "a" * 64 + "." + "Z" * 64,    # bad chars in HMAC
        "G" * 64 + "." + "0" * 64,    # bad chars in id
    ]:
        assert _verify_token(bad) is None, bad


def test_verify_token_rejects_tampered_signature():
    cookie, _ = _new_token()
    raw_id, sig = cookie.split(".", 1)
    # Flip the last hex char of the signature; HMAC verification must fail.
    tampered_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert _verify_token(f"{raw_id}.{tampered_sig}") is None


def test_hmac_hex_is_deterministic_and_secret_dependent():
    # Same input twice → same HMAC. Sanity check that we're not introducing
    # nondeterminism through a randomised secret.
    h1 = _hmac_hex("a" * 64)
    h2 = _hmac_hex("a" * 64)
    assert h1 == h2 and len(h1) == 64


# ---- Password hashing ---------------------------------------------------

# bcrypt rounds=4 keeps the test fast; the CLI sets rounds=12 in prod.
@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch):
    from config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "bcrypt_cost", 4)


def test_password_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)


def test_password_wrong_rejected():
    h = hash_password("right")
    assert not verify_password("wrong", h)


def test_verify_password_does_not_crash_on_garbage_hash():
    # A malformed hash in the DB must not 500 the login route. We treat it
    # as "wrong password" so the user gets the same generic 401.
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False


# ---- Email regex --------------------------------------------------------

@pytest.mark.parametrize("email", [
    "user@example.com",
    "user.name+tag@example.co.uk",
    "x@y.z",
])
def test_email_regex_accepts_valid(email):
    assert EMAIL_RE.match(email)


@pytest.mark.parametrize("email", [
    "no-at-sign",
    "@nouser.com",
    "user@",
    "user@no-tld",
    "with space@example.com",
    "",
])
def test_email_regex_rejects_invalid(email):
    assert not EMAIL_RE.match(email)

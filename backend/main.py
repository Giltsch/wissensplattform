"""FastAPI app: lifespan, middleware, routes.

Routes:
    GET  /health         - liveness probe (no auth)
    POST /api/login      - email + password -> session cookie
    POST /api/logout     - invalidate session
    GET  /api/me         - whoami
    POST /api/chat       - retrieve + stream LLM completion (SSE)
"""
# NB: deliberately no `from __future__ import annotations` here. slowapi's
# @limiter.limit wraps each route in a function defined in slowapi's module,
# and FastAPI/pydantic resolve string annotations against that wrapper's
# globals — where our Pydantic body models do not exist. Concrete annotations
# avoid the ForwardRef resolution path entirely.

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, Field, field_validator
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from rate_limit import limiter
from starlette.middleware.base import BaseHTTPMiddleware

from admin import router as admin_router
from totp import router as totp_router
from conversations import (
    append_message,
    create_for_user,
    fetch_history,
    is_owner,
    maybe_set_title_from_message,
    messages_router,
    router as conversations_router,
)
from auth import (
    SESSION_COOKIE_NAME,
    LoginResult,
    login as auth_login,
    login_with_totp,
    logout as auth_logout,
    require_user,
)
from config import get_settings
from db import apply_migrations, close_pool, connection, init_pool
from rag import UpstreamBusyError, retrieve, stream_completion, warmup as rag_warmup
import cleanup
import telemetry

log = logging.getLogger(__name__)


# ---- Lifespan ------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Initialise tracing before anything else logs interesting work, so
    # those operations show up in spans. Inert if no exporter env is set.
    telemetry.init()
    # CORS + credentials + a wildcard origin = any malicious page can hit
    # the API with the user's cookies. Refuse to start rather than ship
    # that footgun (audit M-5, 2026-05-10).
    if "*" in get_settings().allowed_origin:
        raise RuntimeError(
            "ALLOWED_ORIGIN must be a specific scheme://host[:port], "
            "never a wildcard — CORS allow_credentials=True is on."
        )
    # Migrations create the `vector` extension; must precede init_pool()
    # because the pool's configure callback registers a vector type adapter.
    await apply_migrations()
    await init_pool()
    if not get_settings().cookie_secure:
        # Only safe on plain-HTTP local dev. Loud-on-startup so this isn't
        # silently left unset in production.
        log.warning("COOKIE_SECURE is false; set to true behind HTTPS")
    # Periodic cleanup of expired auth-state rows. Without this, sessions
    # and login_challenges grow forever (audit L-6, 2026-05-10).
    cleanup_task = asyncio.create_task(cleanup.run_periodic())
    # Pre-load embedder + reranker so the first chat after a fresh container
    # doesn't pay the ~10 s cold-start cost. Runs in the background — if it
    # fails the lazy loaders take over on first request.
    warmup_task = asyncio.create_task(rag_warmup())
    log.info("startup complete")
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        # Warmup is best-effort; if it's still running on shutdown
        # cancel it and await the CancelledError so we don't drop a
        # "Task was destroyed but it is pending!" warning at exit
        # (audit C5-MED-6, 2026-05-12).
        warmup_task.cancel()
        try:
            await warmup_task
        except (asyncio.CancelledError, Exception):
            pass
        await close_pool()


# ---- App + middleware ----------------------------------------------------

app = FastAPI(
    title="Internal Support RAG",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# Wrap the configured app object with the FastAPI auto-instrumentation so
# every HTTP request gets a top-level span. Done at module load (not in
# lifespan) because the instrumentor patches routes on the app instance.
telemetry.instrument_app(app)

# Per-IP rate limiter (instantiated in rate_limit.py so totp.py and other
# routers can share it without an import cycle). Routes opt in with
# @limiter.limit("..."). slowapi reads request.client.host, which is only
# the real client IP because the Dockerfile passes --forwarded-allow-ips '*'
# so uvicorn rewrites it from nginx's X-Forwarded-For (audit H-1, 2026-05-10).
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    # Return a generic 429 — slowapi's default leaks the limit string.
    return JSONResponse({"detail": "too many requests"}, status_code=429)


app.add_middleware(SlowAPIMiddleware)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.allowed_origin],
    allow_credentials=True,
    # Match the verb-set the API actually exposes — conversations.py and
    # admin.py use PATCH/DELETE; the previous GET/POST-only list would
    # silently fail cross-origin tools (audit C2-LOW-9, 2026-05-12).
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
    max_age=600,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defense-in-depth response headers. CSP is restrictive on purpose."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        # CSP: only same-origin assets, no inline scripts, no eval.
        # The frontend ships an external app.js (no inline) so this is fine.
        # `object-src 'none'` and `frame-src 'none'` close two `default-src`
        # inheritance edge cases CSP auditors flag (audit C2-MED-4,
        # 2026-05-12). `require-trusted-types-for 'script'` is a tripwire:
        # the current code uses textContent/createElement and would pass
        # silently, but the moment a future bugfix introduces innerHTML
        # the browser will refuse it instead of executing.
        h["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "frame-src 'none'; "
            "worker-src 'self'; "
            "manifest-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "require-trusted-types-for 'script'; "
            # Browsers POST violations to this endpoint (legacy report-uri
            # is still honoured by Chrome/Firefox/Safari; newer report-to
            # would need a Reporting-Endpoints header — the legacy form
            # covers our needs without that ceremony). See main.csp_report.
            "report-uri /api/csp-report"
        )
        # Strip the framework's identifier — minor disclosure hardening.
        h["Server"] = "internal"
        # Authenticated JSON must not be cached by intermediaries. Without
        # this header a corporate proxy with default heuristics can serve
        # one user's /api/me to the next user on the same shared machine
        # (audit M-2, 2026-05-10). The chat SSE response sets its own
        # Cache-Control via StreamingResponse, so guard with setdefault.
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "private, no-store")
        # HSTS only when we believe TLS is in front (signal: cookie_secure
        # is on). Caddy adds its own in prod; this covers operators who
        # terminate TLS elsewhere and forget to configure HSTS there
        # (audit L-3).
        if get_settings().cookie_secure:
            h.setdefault(
                "Strict-Transport-Security",
                "max-age=63072000; includeSubDomains",
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ---- Pydantic models -----------------------------------------------------

# Strip ASCII control chars (except \n, \r, \t) — they're never meaningful in
# user-supplied chat queries and have been used in past prompt-injection attacks.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Pattern matching the citation form the system prompt asks the model to
# produce: `[filename.ext]`. Used to detect citation hallucinations
# (filename in the answer that wasn't in the retrieved sources).
# Mirrors the filename allowlist enforced at ingest time
# (admin._SAFE_NAME) so a hallucinated citation that fails the same
# format check won't even register as a citation.
_CITATION_RE = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9._-]{0,80}\.[A-Za-z0-9]{1,8})\]")

# Verbatim opening phrase of the system prompt (see prompts/system_de.j2).
# If this string ever appears in an LLM answer, the model is echoing
# the prompt back — almost certainly because a user asked it to.
# Detected for forensic logging only (audit C5-MED-6, 2026-05-12).
_SYSTEM_PROMPT_SENTINEL = "Du bist ein interner Support-Assistent"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    ok: bool


class LoginTotpRequest(BaseModel):
    challenge_token: str = Field(min_length=8, max_length=200)
    code: str = Field(min_length=6, max_length=10)


class LoginTotpResponse(BaseModel):
    ok: bool


class MeResponse(BaseModel):
    id: int
    email: EmailStr
    is_admin: bool
    # Tag allow-list. Empty for users with only public-document access; the
    # frontend uses it to render an "access scope" hint on the chat header.
    # The server enforces ACLs; this field is informational.
    allowed_tags: list[str]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    # Conversation context lives server-side. The client either points at an
    # existing conversation it owns (verified by user_id), or omits the field
    # and the server creates a fresh one — its id is returned via the SSE
    # `meta` event so the client can subsequently load it.
    conversation_id: Optional[int] = None

    @field_validator("message")
    @classmethod
    def _clean(cls, v: str) -> str:
        cleaned = _CONTROL_RE.sub("", v).strip()
        if not cleaned:
            raise ValueError("empty message")
        return cleaned


# ---- Routes --------------------------------------------------------------

@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---- Branding (public read; admin write lives in admin.py) ---------------

# Neutral white-label defaults. Baked in here (not pulled from the DB) so a
# fresh install — or a DB where the singleton row somehow went missing —
# still renders a complete, unbranded UI instead of 500ing on every page
# load. The logo is a CSP-safe inline SVG data-URI (img-src 'self' data:
# already permits it). Keep in lockstep with the neutral defaults in
# migration 014 and frontend/logo.svg.
_NEUTRAL_LOGO_DATA_URI = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAg"
    "MCAzMiAzMiIgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiBmaWxsPSJub25lIj48cmVjdCB4"
    "PSI1IiB5PSI0IiB3aWR0aD0iMjIiIGhlaWdodD0iMjQiIHJ4PSIzIiBmaWxsPSIjMjU2"
    "M0VCIiBvcGFjaXR5PSIwLjEyIi8+PHBhdGggZD0iTTkgOWgxNE05IDE0aDE0TTkgMTlo"
    "OSIgc3Ryb2tlPSIjMjU2M0VCIiBzdHJva2Utd2lkdGg9IjIiIHN0cm9rZS1saW5lY2Fw"
    "PSJyb3VuZCIvPjwvc3ZnPg=="
)

_BRANDING_DEFAULTS = {
    "app_name": "Wissensplattform",
    "sub_brand": "",
    "accent_color": "#2563EB",
    "tagline": "",
    "logo_data_uri": _NEUTRAL_LOGO_DATA_URI,
    "company_legal_name": "",
    "company_address": "",
    "company_representative": "",
    "contact_email": "",
    "privacy_email": "",
    "register_info": "",
    "vat_id": "",
    "supervisory_authority": "",
}


class BrandingOut(BaseModel):
    app_name: str
    sub_brand: str
    accent_color: str
    tagline: str
    # White-label additions (migration 014). The logo is an inline data-URI
    # (empty string falls back to the neutral baked-in mark client-side); the
    # remaining fields are the operator legal identity rendered into the
    # impressum/datenschutz pages.
    logo_data_uri: str
    company_legal_name: str
    company_address: str
    company_representative: str
    contact_email: str
    privacy_email: str
    register_info: str
    vat_id: str
    supervisory_authority: str


@app.get("/api/branding", response_model=BrandingOut)
async def get_branding() -> BrandingOut:
    """Public — the login page (and the legal pages) need the brand before
    the user has authed. Reveals only display/legal text; no sensitive data."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT app_name, sub_brand, accent_color, tagline, "
                "       logo_data_uri, company_legal_name, company_address, "
                "       company_representative, contact_email, privacy_email, "
                "       register_info, vat_id, supervisory_authority "
                "FROM branding WHERE id = 1"
            )
            row = await cur.fetchone()
    # Defensive: if the seed somehow missing, fall back to neutral defaults
    # rather than 500ing on every page load.
    if row is None:
        return BrandingOut(**_BRANDING_DEFAULTS)
    # An empty logo_data_uri means "operator never uploaded one" — serve the
    # neutral baked-in mark so the UI always has a logo.
    if not row.get("logo_data_uri"):
        row = dict(row)
        row["logo_data_uri"] = _NEUTRAL_LOGO_DATA_URI
    return BrandingOut(**row)


@app.post("/api/login", response_model=LoginResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    body: LoginRequest = Body(...),
) -> LoginResponse:
    settings = get_settings()
    ip = get_remote_address(request)
    result, payload = await auth_login(body.email, body.password, ip)

    # Generic 401 for every failure case — same shape so the response
    # doesn't leak whether the email exists, the account is locked, etc.
    if result == LoginResult.BAD or result == LoginResult.LOCKED or result == LoginResult.INACTIVE:
        log.info("login_fail ip=%s reason=%s email_len=%d", ip, result, len(body.email))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    # Password verified, but the user has 2FA. Hand back a challenge token
    # and tell the client to call /api/login/totp with the second factor.
    # 202 Accepted = "the request was understood and accepted but completion
    # requires another step", which fits this transition exactly.
    if result == LoginResult.TOTP_REQUIRED:
        log.info("login_totp_pending ip=%s", ip)
        return JSONResponse(
            {"totp_required": True, "challenge_token": payload},
            status_code=status.HTTP_202_ACCEPTED,
        )

    # OK + cookie: full session granted (no 2FA on this account).
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=payload,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    log.info("login_ok ip=%s", ip)
    return LoginResponse(ok=True)


@app.post("/api/login/totp", response_model=LoginTotpResponse)
@limiter.limit("10/minute")
async def login_totp(
    request: Request,
    response: Response,
    body: LoginTotpRequest = Body(...),
) -> LoginTotpResponse:
    """Second login step. Expects the challenge_token from /api/login plus
    a valid TOTP code. On success: real session cookie set, challenge
    consumed (single-use)."""
    settings = get_settings()
    ip = get_remote_address(request)
    result, cookie = await login_with_totp(body.challenge_token, body.code, ip)
    if result != LoginResult.OK or cookie is None:
        log.info("login_totp_fail ip=%s reason=%s", ip, result)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    log.info("login_totp_ok ip=%s", ip)
    return LoginTotpResponse(ok=True)


@app.post("/api/csp-report", status_code=204)
@limiter.limit("30/minute")
async def csp_report(request: Request) -> Response:
    """Receive CSP violation reports from browsers (audit I-5, 2026-05-10).

    Unauthenticated by design — the browser sends these without cookies,
    so anyone can POST. Body is capped at 4 KB and the per-IP rate
    limiter caps the volume; the only side effect is a log line. We do
    not persist reports — Postgres growth from a misbehaving page would
    be self-inflicted DoS. Operators tail backend logs to find
    legitimate-but-blocked resources and probe attempts.

    Accepts both legacy ``application/csp-report`` (Chrome/Firefox
    ``report-uri``) and newer ``application/reports+json`` (modern
    ``report-to``). The payload is logged as-is — no parsing beyond
    "is this valid JSON".
    """
    raw = await request.body()
    if len(raw) > 4096:
        # Browsers send small reports (a few hundred bytes). Anything
        # bigger is suspicious / hostile — log the size only, drop body.
        log.info("csp_report dropped oversize bytes=%d ip=%s",
                 len(raw), get_remote_address(request))
        return Response(status_code=204)
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        log.info("csp_report malformed bytes=%d", len(raw))
        return Response(status_code=204)
    # Re-serialise with ensure_ascii=True so Unicode line/paragraph
    # separators (U+2028, U+2029) in attacker-supplied payload values
    # can't break out of the log line into a fake follow-up entry
    # (audit C4-MED-11, 2026-05-12). Cap at 2 KB so a vanishingly-small
    # CSP report fits in one log line; bigger payloads are already
    # rejected above at 4 KB body cap.
    payload_str = json.dumps(payload, ensure_ascii=True)[:2048]
    # Log at INFO — quiet enough for normal ops, visible in `docker compose
    # logs backend | grep csp_report`. Operators decide whether to ship
    # these to their log aggregator with a filter.
    log.info("csp_report ip=%s payload=%s",
             get_remote_address(request), payload_str)
    return Response(status_code=204)


@app.post("/api/logout")
async def logout(request: Request, response: Response) -> JSONResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        await auth_logout(cookie)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return JSONResponse({"ok": True})


@app.get("/api/me", response_model=MeResponse)
async def me(user: dict = Depends(require_user)) -> MeResponse:
    return MeResponse(
        id=user["id"],
        email=user["email"],
        is_admin=user.get("is_admin", False),
        allowed_tags=user.get("allowed_tags", []),
    )


# ---- DSAR: data subject access requests (DSGVO Art. 15 / 17 / 20) --------
# Self-service so the operator doesn't have to run psql on every request.
# `password_hash` and `totp_secret` are deliberately excluded from the
# export: they are security artefacts (credential material), not the
# personal data Art. 15 entitles the subject to a copy of, and disclosing
# them would weaken the account they belong to.

def _iso(v):
    return v.isoformat() if v is not None else None


@app.get("/api/me/export")
@limiter.limit("5/hour")
async def export_my_data(
    request: Request,
    user: dict = Depends(require_user),
) -> Response:
    """JSON dump of everything stored about the calling user. Satisfies the
    right of access (Art. 15) and provides a machine-readable copy
    (Art. 20). Limited to 5/hour to prevent abuse of the dump endpoint
    on long histories."""
    uid = user["id"]
    out: dict = {
        "export_format_version": 1,
        "generated_at": None,  # filled after the SELECT now()
        "subject_id": uid,
    }
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT now() AS now")
            out["generated_at"] = _iso((await cur.fetchone())["now"])

            await cur.execute(
                "SELECT id, email, is_active, is_admin, allowed_tags, "
                "       totp_enabled, created_at "
                "FROM users WHERE id = %s",
                (uid,),
            )
            u = await cur.fetchone()
            out["account"] = {
                "id": u["id"],
                "email": u["email"],
                "is_active": u["is_active"],
                "is_admin": u["is_admin"],
                "allowed_tags": u["allowed_tags"],
                "totp_enabled": u["totp_enabled"],
                "created_at": _iso(u["created_at"]),
            }

            await cur.execute(
                "SELECT id, title, created_at, updated_at "
                "FROM conversations WHERE user_id = %s "
                "ORDER BY created_at",
                (uid,),
            )
            convs = await cur.fetchall()

            await cur.execute(
                "SELECT m.id, m.conversation_id, m.role, m.content, "
                "       m.sources, m.created_at, "
                "       fb.rating AS fb_rating, fb.comment AS fb_comment, "
                "       fb.created_at AS fb_created_at "
                "FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "LEFT JOIN message_feedback fb ON fb.message_id = m.id "
                "WHERE c.user_id = %s "
                "ORDER BY m.conversation_id, m.created_at, m.id",
                (uid,),
            )
            msgs = await cur.fetchall()

            await cur.execute(
                "SELECT day, request_count FROM chat_usage "
                "WHERE user_id = %s ORDER BY day",
                (uid,),
            )
            usage = await cur.fetchall()

            await cur.execute(
                "SELECT occurred_at, ip, success "
                "FROM login_attempts WHERE email = %s "
                "ORDER BY occurred_at",
                (user["email"],),
            )
            attempts = await cur.fetchall()

            audit_rows = []
            if user.get("is_admin"):
                await cur.execute(
                    "SELECT id, action, target_type, target_id, "
                    "       details, ip, occurred_at "
                    "FROM admin_audit WHERE actor_id = %s "
                    "ORDER BY occurred_at",
                    (uid,),
                )
                audit_rows = await cur.fetchall()

    by_conv: dict[int, list] = {}
    for m in msgs:
        by_conv.setdefault(m["conversation_id"], []).append({
            "id": m["id"],
            "role": m["role"],
            "content": m["content"],
            "sources": m["sources"],
            "created_at": _iso(m["created_at"]),
            "feedback": (
                {
                    "rating": m["fb_rating"],
                    "comment": m["fb_comment"],
                    "created_at": _iso(m["fb_created_at"]),
                }
                if m["fb_rating"] is not None else None
            ),
        })
    out["conversations"] = [
        {
            "id": c["id"],
            "title": c["title"],
            "created_at": _iso(c["created_at"]),
            "updated_at": _iso(c["updated_at"]),
            "messages": by_conv.get(c["id"], []),
        }
        for c in convs
    ]
    out["chat_usage"] = [
        {"day": _iso(r["day"]), "request_count": r["request_count"]}
        for r in usage
    ]
    out["login_attempts"] = [
        {
            "occurred_at": _iso(r["occurred_at"]),
            "ip": r["ip"],
            "success": r["success"],
        }
        for r in attempts
    ]
    if user.get("is_admin"):
        out["admin_audit_as_actor"] = [
            {
                "id": r["id"],
                "action": r["action"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "details": r["details"],
                "ip": r["ip"],
                "occurred_at": _iso(r["occurred_at"]),
            }
            for r in audit_rows
        ]

    log.info("dsar_export user_id=%s convs=%d msgs=%d",
             uid, len(convs), len(msgs))
    body = json.dumps(out, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="dsar-export-user-{uid}.json"',
        },
    )


@app.delete("/api/me", status_code=204)
@limiter.limit("3/hour")
async def delete_my_account(
    request: Request,
    response: Response,
    user: dict = Depends(require_user),
) -> None:
    """Self-delete the calling account (DSGVO Art. 17). Cascades via FK:
    sessions, conversations, messages, message_feedback, chat_usage,
    login_challenges are removed automatically. `login_attempts` is keyed
    by email (no FK) and is wiped explicitly here.

    Refuses with 409 if the user has admin_audit entries (FK ON DELETE
    RESTRICT, by design from migration 004). Those users must request
    deletion through the operator, who can re-attribute the audit rows
    to a system sentinel first."""
    import psycopg
    uid = user["id"]
    email = user["email"]
    try:
        async with connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM login_attempts WHERE email = %s",
                    (email,),
                )
                await cur.execute("DELETE FROM users WHERE id = %s", (uid,))
            await conn.commit()
    except psycopg.errors.ForeignKeyViolation:
        log.info("dsar_delete_blocked user_id=%s reason=admin_audit", uid)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "account cannot be self-deleted because it has admin "
                "audit entries; contact the operator"
            ),
        )
    # Mutate the dependency-injected Response so FastAPI merges the
    # Set-Cookie header into the 204 reply (same pattern as /api/logout).
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    log.info("dsar_delete user_id=%s", uid)


# ---- Read-only document browsing (any authed user) ------------------------
# Listing and the full-text view are filtered by the same ACL that gates
# retrieval (see acls.visibility_clause + migration 011). Without the
# filter here a user could drill into a chunk via /api/documents/{id}
# even if retrieve() correctly hid that chunk in the chat — that would
# be a backdoor. Mutating endpoints stay under /api/admin/*.
from acls import visibility_clause as _visibility_clause


@app.get("/api/documents")
async def list_documents_public(user: dict = Depends(require_user)) -> list[dict]:
    """List the current version of every document the caller can see.
    Archived versions never appear; they aren't reachable from the chat
    either."""
    acl_sql, acl_params = _visibility_clause(
        user.get("is_admin", False), user.get("allowed_tags", []),
    )
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, source, version, origin, ingested_at "
                "FROM documents AS d "
                f"WHERE d.is_current = TRUE AND {acl_sql} "
                "ORDER BY source",
                tuple(acl_params),
            )
            rows = await cur.fetchall()
    return [
        {
            "id": r["id"], "source": r["source"], "version": r["version"],
            "origin": r["origin"],
            "ingested_at": r["ingested_at"].isoformat(),
        }
        for r in rows
    ]


@app.get("/api/documents/{document_id}")
async def get_document(
    document_id: int,
    user: dict = Depends(require_user),
) -> dict:
    """Return a document's full text (chunks in order). Works for any
    document_id the caller is allowed to see — including archived
    versions of permitted documents (useful for inspecting the state a
    historical answer was based on). 404 covers both "doesn't exist"
    and "not visible to you" so we don't leak existence."""
    acl_sql, acl_params = _visibility_clause(
        user.get("is_admin", False), user.get("allowed_tags", []),
    )
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, source, version, origin, is_current, ingested_at "
                "FROM documents AS d "
                f"WHERE id = %s AND {acl_sql}",
                (document_id, *acl_params),
            )
            doc = await cur.fetchone()
            if doc is None:
                raise HTTPException(status_code=404, detail="document not found")
            await cur.execute(
                "SELECT chunk_index, content FROM chunks "
                "WHERE document_id = %s ORDER BY chunk_index",
                (document_id,),
            )
            chunks = await cur.fetchall()
    return {
        "id": doc["id"],
        "source": doc["source"],
        "version": doc["version"],
        "origin": doc["origin"],
        "is_current": doc["is_current"],
        "ingested_at": doc["ingested_at"].isoformat(),
        "chunks": [
            {"chunk_index": c["chunk_index"], "content": c["content"]}
            for c in chunks
        ],
    }


# ---- /api/chat -----------------------------------------------------------

def _sse(event: str, payload: dict) -> bytes:
    # Newlines in payload would break the SSE framing — JSON-encode escapes them.
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


_SNIPPET_MAX_CHARS = 400


async def _cleanup_hollow_conversation(conv_id: int, is_new: bool) -> None:
    """Delete a brand-new conversation that has only the user turn.

    A NEW conversation that fails before the assistant has yielded any
    content would otherwise leave a single-message stub in the sidebar
    (audit C5-MED-8, 2026-05-12). We only delete if the conversation
    has NO assistant message — preserves any prior history if the
    caller passed an existing conversation_id whose stream failed.
    """
    if not is_new:
        return
    try:
        async with connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM conversations WHERE id = %s AND NOT EXISTS ("
                    "  SELECT 1 FROM messages WHERE conversation_id = %s "
                    "  AND role = 'assistant'"
                    ")",
                    (conv_id, conv_id),
                )
            await conn.commit()
    except Exception:
        # Best-effort cleanup. The cleanup.py retention sweep will
        # collect stragglers if this hiccups.
        log.exception("hollow conv cleanup failed conv=%s", conv_id)


def _truncate_snippet(text: str) -> str:
    # Collapse runs of whitespace introduced by chunk concat, then cap at
    # _SNIPPET_MAX_CHARS at the nearest space so we don't slice mid-word.
    s = " ".join(text.split())
    if len(s) <= _SNIPPET_MAX_CHARS:
        return s
    cut = s.rfind(" ", 0, _SNIPPET_MAX_CHARS)
    if cut < _SNIPPET_MAX_CHARS - 50:
        cut = _SNIPPET_MAX_CHARS
    return s[:cut].rstrip() + "…"


@app.post("/api/chat")
@limiter.limit("30/minute")
async def chat(
    request: Request,
    user: dict = Depends(require_user),
    body: ChatRequest = Body(...),
) -> StreamingResponse:
    log.info(
        "chat user_id=%s query_len=%d conv=%s",
        user["id"], len(body.message), body.conversation_id,
    )

    settings = get_settings()

    # Verify conversation ownership BEFORE charging the quota — otherwise
    # an attacker with a stolen session can burn another user's daily
    # budget by probing non-existent conversation_ids and collecting 404s
    # (audit C5-HIGH-3, 2026-05-12). Conversation creation is also moved
    # below the quota check so a 429 doesn't leave a dangling empty
    # conversation behind (audit C5-MEDIUM-7).
    if body.conversation_id is not None:
        if not await is_owner(body.conversation_id, user["id"]):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="conversation not found",
            )

    # Per-user daily budget. UPSERT increments today's row; a fresh day starts
    # with a new row at 1. We count every call that gets past ownership +
    # auth — the rewrite step costs OpenRouter tokens too, so retrieval-only
    # failures still consume real budget and should count.
    #
    # Concurrency note: `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`
    # is atomic per-row in Postgres — concurrent increments take a tuple
    # lock and each sees a strictly monotonic `request_count`. The
    # check below uses the value freshly RETURNINGed, so a TOCTOU split
    # between "read then check" is impossible at this layer (audit
    # C3-MED-2, 2026-05-12).
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO chat_usage(user_id, day, request_count) "
                "VALUES (%s, CURRENT_DATE, 1) "
                "ON CONFLICT (user_id, day) DO UPDATE "
                "  SET request_count = chat_usage.request_count + 1 "
                "RETURNING request_count",
                (user["id"],),
            )
            row = await cur.fetchone()
        await conn.commit()
    today_count = row["request_count"]
    if today_count > settings.chat_daily_limit_per_user:
        log.info(
            "chat_quota_block user_id=%s day_count=%d limit=%d",
            user["id"], today_count, settings.chat_daily_limit_per_user,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="daily quota exceeded",
        )

    # Quota is committed — now create the conversation if needed.
    if body.conversation_id is not None:
        conv_id = body.conversation_id
        is_new = False
    else:
        conv_id = await create_for_user(user["id"])
        is_new = True

    # Snapshot prior turns BEFORE persisting the new user message; otherwise
    # the rewrite/retrieval would see the latest question twice.
    prior_history = await fetch_history(conv_id)
    # Apply the same client-side bounds the old API used so prompt budget
    # stays predictable on long-running threads.
    if len(prior_history) > settings.max_history_turns:
        prior_history = prior_history[-settings.max_history_turns:]
    total = sum(len(m["content"]) for m in prior_history)
    while prior_history and total > settings.max_history_chars:
        total -= len(prior_history[0]["content"])
        prior_history = prior_history[1:]

    # Persist user turn now — even if retrieval crashes, the user's question
    # is preserved in their history.
    await append_message(conv_id, "user", body.message)
    if is_new:
        await maybe_set_title_from_message(conv_id, body.message)

    # Wider span around the streaming generator. The FastAPI auto-instrumentation
    # span ends with the response — but for a *streamed* response that's still
    # the right boundary, so this child span captures the chat-specific work
    # without doubling the surface. Only metadata in attributes; never the
    # query text or document content (SECURITY.md A09).
    chat_tracer = telemetry.get_tracer("chat")
    span_attrs = {
        "chat.user_id": user["id"],
        "chat.conversation_id": conv_id,
        "chat.is_admin": bool(user.get("is_admin", False)),
        "chat.allowed_tags_count": len(user.get("allowed_tags", []) or []),
        "chat.history_turns": len(prior_history),
        "chat.is_new_conversation": is_new,
        "chat.query_len": len(body.message),
    }

    async def event_stream() -> AsyncIterator[bytes]:
        # Always emit the conversation id first. The frontend uses it to
        # adopt a server-created conversation into its sidebar state.
        yield _sse("meta", {"conversation_id": conv_id, "is_new": is_new})

        captured_sources: Optional[list[dict]] = None
        buffered = ""
        try:
            with chat_tracer.start_as_current_span("chat.stream", attributes=span_attrs) as chat_span:
                hits = await retrieve(
                    body.message,
                    is_admin=user.get("is_admin", False),
                    allowed_tags=user.get("allowed_tags", []),
                    history=prior_history,
                )
                chat_span.set_attribute("chat.hits", len(hits))
                captured_sources = [
                    {
                        "source": h.source,
                        "chunk_index": h.chunk_index,
                        "document_id": h.document_id,
                        "text": _truncate_snippet(h.content),
                    }
                    for h in hits
                ]
                yield _sse("sources", {"sources": captured_sources})

                # Time-to-first-token is the user-visible latency metric;
                # tracked as a span event + attribute so dashboards can plot it.
                import time as _time
                t_start = _time.monotonic()
                first_token_ms: Optional[float] = None
                token_count = 0
                with chat_tracer.start_as_current_span("chat.llm.stream") as llm_span:
                    # Provider name only (metadata) — never query text (A09).
                    llm_span.set_attribute("llm.provider", settings.llm_provider)
                    client_gone = False
                    async for token in stream_completion(body.message, hits, history=prior_history):
                        # Cheap poll: if the client closed the connection
                        # (tab closed, network drop) we stop iterating
                        # the upstream stream so OpenRouter can stop
                        # billing us for tokens nobody is reading
                        # (audit C5-HIGH-4, 2026-05-12). The partial
                        # `buffered` is still persisted below so reload
                        # shows what the user actually saw.
                        if await request.is_disconnected():
                            client_gone = True
                            log.info(
                                "chat client_disconnect user_id=%s conv=%s tokens=%d",
                                user["id"], conv_id, token_count,
                            )
                            llm_span.set_attribute("llm.client_disconnect", True)
                            break
                        if first_token_ms is None:
                            first_token_ms = (_time.monotonic() - t_start) * 1000.0
                            llm_span.add_event("first_token", attributes={
                                "first_token_ms": first_token_ms,
                            })
                        token_count += 1
                        buffered += token
                        yield _sse("token", {"text": token})
                    llm_span.set_attribute("llm.tokens_streamed", token_count)
                    if first_token_ms is not None:
                        llm_span.set_attribute("llm.first_token_ms", first_token_ms)
                    if client_gone:
                        # Exit the outer span path cleanly so we still
                        # persist what we got, but skip the `done` SSE
                        # — nobody's listening.
                        if buffered.strip():
                            try:
                                await append_message(
                                    conv_id, "assistant", buffered, captured_sources,
                                )
                            except Exception:
                                log.exception(
                                    "persist partial assistant msg failed conv=%s",
                                    conv_id,
                                )
                        return
        except UpstreamBusyError as e:
            # OpenRouter (or its upstream) is rate-limiting or overloaded
            # even after retries + optional fallback model. Surface this
            # to the user with a distinct detail string so the frontend
            # can render a "please try again" hint instead of the generic
            # error.
            log.warning(
                "chat upstream_busy user_id=%s conv=%s status=%s",
                user["id"], conv_id, e.status_code,
            )
            yield _sse("error", {"detail": "upstream_busy"})
            await _cleanup_hollow_conversation(conv_id, is_new)
            return
        except Exception:
            log.exception("chat stream failed user_id=%s conv=%s", user["id"], conv_id)
            yield _sse("error", {"detail": "internal error"})
            await _cleanup_hollow_conversation(conv_id, is_new)
            return

        # System-prompt-exfiltration check (audit C5-MED-6, 2026-05-12).
        # The system prompt is semi-public by design (it contains no
        # secrets, only behaviour rules — OWASP LLM06's stance), but a
        # successful echo back to the user is a useful signal that
        # someone is probing the boundary. Match a sentinel that
        # appears verbatim in the system prompt; surface in logs only.
        if buffered and _SYSTEM_PROMPT_SENTINEL in buffered:
            log.warning(
                "chat system_prompt_echo user_id=%s conv=%s",
                user["id"], conv_id,
            )

        # Citation hallucination check (audit C5-MED-5, 2026-05-12).
        # Frontend already refuses to render `[filename]` as a clickable
        # source unless it's in the captured_sources set; that hides
        # the bug from users but doesn't tell US when the model is
        # citing fiction. Log occurrences with metadata only so we can
        # spot a model regression in dashboards (NEVER the actual
        # citation text — that may leak doc names from the corpus
        # filtered out by ACL).
        if buffered:
            known = {s["source"] for s in (captured_sources or [])}
            cited = set(_CITATION_RE.findall(buffered))
            hallucinated = cited - known
            if hallucinated:
                log.warning(
                    "chat citation_hallucination user_id=%s conv=%s "
                    "real=%d fake=%d",
                    user["id"], conv_id, len(cited & known), len(hallucinated),
                )

        # Persist the assistant turn only if we got real content. An empty
        # stream shouldn't leave a hollow row that then gets replayed as
        # history on the next turn. The message id is sent with `done` so
        # the frontend can attach feedback buttons immediately.
        assistant_message_id: Optional[int] = None
        if buffered.strip():
            try:
                assistant_message_id = await append_message(
                    conv_id, "assistant", buffered, captured_sources,
                )
            except Exception:
                log.exception("persist assistant msg failed conv=%s", conv_id)
        yield _sse("done", {"message_id": assistant_message_id})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # Hint to any intermediate proxy that this is a streaming response.
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


# ---- Admin --------------------------------------------------------------
# Routes under /api/admin/*; each requires `require_admin` and returns
# 401 (no session) or 403 (session but not admin).
app.include_router(admin_router)

# ---- Conversations + per-message feedback -------------------------------
# /api/conversations/* — CRUD on conversations. /api/messages/* — feedback
# (👍/👎) on individual assistant turns. Both strictly user-scoped.
app.include_router(conversations_router)
app.include_router(messages_router)

# ---- 2FA self-service ---------------------------------------------------
# /api/me/2fa/* — every endpoint require_user, scoped to the calling user.
app.include_router(totp_router)

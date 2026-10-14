"""Admin endpoints: document upload + user management.

Mounted under /api/admin/*. Every route depends on `require_admin`, which
returns 401 (no session) or 403 (session but not admin). The frontend's
admin tab is gated by the `is_admin` flag in /api/me.
"""
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from slowapi.util import get_remote_address

from acls import assert_known_departments, normalise_tags
from auth import EMAIL_RE, hash_password, require_admin
from db import connection
from rate_limit import limiter
# NB: no `from __future__ import annotations` in this module — slowapi's
# @limiter.limit wraps each route in a function whose __globals__ don't
# see our Pydantic body models, so FastAPI would reclassify them as
# query params if annotations were stringified. Same gotcha as main.py.
from ingest import (
    Embedder,
    EmptyDocumentError,
    ParseError,
    SourceItem,
    _document_already_ingested,
    ingest_item,
)
from connectors import connector_factory
from config import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

# Where the ingester reads from. Mounted as /app/docs in docker-compose.
DOCS_DIR = Path("/app/docs")

# Hard caps to prevent abuse. 20MB matches a reasonable PDF; tighter than
# nginx's client_max_body_size on the API path so the backend rejects first.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
SUPPORTED_SUFFIXES = {".pdf", ".md", ".markdown", ".txt"}

# Filename allowlist: alnum, dash, underscore, dot. Prevents path traversal
# (e.g. "../etc/passwd"), shell-special chars, and weird unicode. The first
# char must be alnum (not `.` or `-`) so we don't accept `..pdf`,
# `.env.txt`, `-rf`, etc. (audit M-3, 2026-05-10).
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# The upload handler writes archived versions as `<name>.v<n>.bak`. Reject
# user-supplied names that match that shape so an upload cannot collide
# with — and then overwrite via target.write_bytes — a real backup file.
_BACKUP_SUFFIX_RE = re.compile(r"\.v\d+\.bak$")

# PDF parsing caps. A small (≤MAX_UPLOAD_BYTES) PDF can still expand to
# arbitrary text via heavily-compressed page streams, repeated XObjects,
# or a high page count. These are admin-only entrypoints so the threat is
# "rogue admin or compromised admin account" — bounded but worth capping
# so a misclick doesn't OOM the backend (audit M-4).
#
# The actual enforcement now lives in ingest.parse_bytes (the shared parser
# used by both the CLI and this upload path); these constants are kept here
# so the pinned cap tests in tests/test_upload_validation.py keep guarding
# the value, and must stay in lockstep with ingest.MAX_PDF_PAGES /
# ingest.MAX_PDF_TEXT_CHARS.
MAX_PDF_PAGES = 500
MAX_PDF_TEXT_CHARS = 5_000_000

# One shared embedder for the admin upload path. Loaded lazily on first use
# to keep startup fast. Same model the CLI ingester uses.
_embedder: Optional[Embedder] = None


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder(get_settings().embedding_model)
    return _embedder


import asyncio as _asyncio  # alias to avoid colliding with the existing asyncio import path style
import hashlib  # used below for in-memory file hashing
import struct


async def _clamd_scan(payload: bytes) -> Optional[str]:
    """Scan bytes via clamd's INSTREAM protocol over TCP.

    Returns None if clean, or the virus signature name if a hit is found.
    Raises RuntimeError on protocol or connection errors — callers must
    decide whether that's fatal (fail-closed) or skip-with-log
    (fail-open). For the upload handler we go fail-closed: better to
    refuse an upload than wave malware through during a clamd outage.
    """
    s = get_settings()
    try:
        reader, writer = await _asyncio.wait_for(
            _asyncio.open_connection(s.clamav_host, s.clamav_port),
            timeout=s.clamav_timeout_s,
        )
    except (OSError, _asyncio.TimeoutError) as e:
        raise RuntimeError(f"clamd connection failed: {e}") from e

    try:
        # Protocol: nINSTREAM\n then chunks of [4-byte BE length][bytes],
        # terminated by a zero-length chunk. Response is one line.
        writer.write(b"nINSTREAM\n")
        # Send in 64 KB chunks — keeps memory bounded and matches what
        # clamd is comfortable with on the read side.
        for i in range(0, len(payload), 65536):
            chunk = payload[i:i + 65536]
            writer.write(struct.pack("!I", len(chunk)))
            writer.write(chunk)
        writer.write(struct.pack("!I", 0))
        await writer.drain()
        line = await _asyncio.wait_for(reader.readline(), timeout=s.clamav_timeout_s)
    except _asyncio.TimeoutError as e:
        raise RuntimeError("clamd read timeout") from e
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    text = line.decode("utf-8", errors="replace").strip()
    # Expected: "stream: OK" or "stream: <Sig.Name> FOUND" or "stream: ERROR"
    if text.endswith("OK"):
        return None
    if text.endswith("FOUND"):
        # Signature name sits between "stream: " and " FOUND".
        return text[len("stream: "):-len(" FOUND")].strip() or "unknown"
    raise RuntimeError(f"clamd unexpected response: {text!r}")


# ---- Audit -----------------------------------------------------------------

def _truncate_ip(ip: Optional[str]) -> Optional[str]:
    """Reduce an IP to /24 (v4) or /48 (v6) for storage.

    Forensically still useful ("a request from /24 X did Y at T") but
    falls below the precision that would let a court argue the row is
    full personal data under EuGH C-582/14 (audit C6-MED-11,
    2026-05-12). The function lives next to `_audit` because that's the
    one place admin-action IPs land in the DB; for login_attempts the
    full IP is justified by Art. 6(1)(f) IT-security interest and
    bounded by the retention sweep.
    """
    if not ip:
        return None
    try:
        import ipaddress
        addr = ipaddress.ip_address(ip)
        if isinstance(addr, ipaddress.IPv4Address):
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
        else:
            net = ipaddress.ip_network(f"{ip}/48", strict=False)
        return str(net)
    except (ValueError, ImportError):
        # Don't store an IP we can't parse — better to drop the field
        # than write something that bypasses the truncation.
        return None


async def _audit(
    actor_id: int,
    action: str,
    target_type: str,
    target_id: Optional[int],
    details: dict[str, Any],
    request: Optional[Request],
) -> None:
    """Best-effort write to admin_audit. Failure must not roll back the
    caller's action — by the time we get here the mutation is already
    committed. We log the exception and move on.

    `details` should be small (filenames, role flags, etc.) — never the full
    contents of an uploaded document or a password.
    """
    ip = _truncate_ip(get_remote_address(request)) if request is not None else None
    try:
        async with connection() as conn:
            async with conn.cursor() as cur:
                # psycopg3 serialises Python dicts to JSONB natively when the
                # column type is jsonb — no manual json.dumps needed. We pass
                # via Json() to be explicit and avoid surprises if a future
                # adapter change reinterprets dicts as composite types.
                from psycopg.types.json import Json

                await cur.execute(
                    "INSERT INTO admin_audit("
                    "  actor_id, action, target_type, target_id, details, ip"
                    ") VALUES (%s, %s, %s, %s, %s, %s)",
                    (actor_id, action, target_type, target_id, Json(details), ip),
                )
            await conn.commit()
    except Exception:
        # Action already happened. Surface in logs for ops, but never fail
        # the request just because the audit insert hiccuped.
        log.exception(
            "audit_write_failed action=%s target=%s/%s",
            action, target_type, target_id,
        )


# ---- Models --------------------------------------------------------------

class UserOut(BaseModel):
    id: int
    email: EmailStr
    is_active: bool
    is_admin: bool
    created_at: str
    # Tags grant access to non-public documents (see acls.visibility_clause).
    # Empty list = the user only sees public (untagged) documents.
    allowed_tags: list[str] = Field(default_factory=list)


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    is_admin: bool = False
    allowed_tags: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("allowed_tags")
    @classmethod
    def _norm_tags(cls, v: list[str]) -> list[str]:
        # Same normaliser as the document path so an admin can copy/paste
        # the same string into either form.
        return normalise_tags(v)


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class UpdateUserRequest(BaseModel):
    is_admin: Optional[bool] = None
    is_active: Optional[bool] = None


class UpdateUserTagsRequest(BaseModel):
    """Replace the user's allowed_tags wholesale. Cap at 64 to bound the
    array fed into the GIN-indexed overlap query."""
    allowed_tags: list[str] = Field(max_length=64)

    @field_validator("allowed_tags")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        return normalise_tags(v)


class UpdateDocumentTagsRequest(BaseModel):
    """Replace the document's tags wholesale. Same shape as the user
    counterpart for symmetry."""
    tags: list[str] = Field(max_length=64)

    @field_validator("tags")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        return normalise_tags(v)


class DocumentOut(BaseModel):
    id: int
    source: str
    ingested_at: str
    chunks: int
    version: int
    archived_versions: int
    tags: list[str] = Field(default_factory=list)


class SourceSyncRequest(BaseModel):
    """Drive an external source connector once. v1 ships URL-only: the URL
    list is supplied inline (no saved-sources table yet).

    ``tags`` is the REQUIRED explicit ACL decision for every document fetched
    in this run — external sources are NEVER auto-public and tags are NEVER
    derived from URL structure. ``[]`` deliberately makes the docs public; a
    non-empty list is validated against the departments vocabulary in the
    handler. Omitting ``tags`` (``None``) is rejected with a 400 so a fetched
    URL can never silently default to public the way an inherited upload can.
    """

    type: str = Field(default="url")
    # Bounded to keep one sync request cheap and the SSRF/clamd work bounded.
    urls: list[str] = Field(min_length=1, max_length=100)
    tags: Optional[list[str]] = Field(default=None, max_length=64)

    @field_validator("type")
    @classmethod
    def _only_url(cls, v: str) -> str:
        if v != "url":
            raise ValueError("only type 'url' is supported in v1")
        return v

    @field_validator("tags")
    @classmethod
    def _norm_tags(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        # None stays None (inherit/public); a list is normalised like every
        # other tag-write surface. Vocabulary validation happens in the
        # handler (it needs a DB connection).
        if v is None:
            return None
        return normalise_tags(v)


# ---- Branding (admin write) ----------------------------------------------

# Validates 3- and 6-digit hex colors. Anchored, no extra characters; we
# inject this directly into a CSS variable so a permissive regex would
# create a CSS-injection footgun (despite our strict CSP).
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Logo upload: only inline data-URIs in two image formats are accepted. SVG
# is rendered via <img src> (which cannot execute scripts the way an inline
# <svg> in the DOM could) and PNG is inert. The ~256 KB cap is generous for a
# logo but stops a multi-MB blob from bloating every public /api/branding
# response. base64 inflates by ~4/3, so the raw payload is ~190 KB max.
_LOGO_DATA_URI_PREFIXES = (
    "data:image/svg+xml;base64,",
    "data:image/png;base64,",
)
_MAX_LOGO_DATA_URI_BYTES = 256 * 1024


class UpdateBrandingRequest(BaseModel):
    app_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    # sub_brand may be cleared to empty (white-label installs often have no
    # second word), so min_length=0 — unlike app_name which must be non-empty.
    sub_brand: Optional[str] = Field(default=None, max_length=40)
    accent_color: Optional[str] = Field(default=None, max_length=7)
    tagline: Optional[str] = Field(default=None, max_length=200)
    # Inline data-URI logo; validated against the prefix allowlist + size cap
    # in the handler. max_length here is a coarse upper bound (data-URI is
    # base64, so a bit larger than the byte cap) — the precise check is in
    # update_branding so we can return a clear error.
    logo_data_uri: Optional[str] = Field(default=None, max_length=400_000)
    # Operator legal identity rendered into impressum/datenschutz.
    company_legal_name: Optional[str] = Field(default=None, max_length=200)
    company_address: Optional[str] = Field(default=None, max_length=400)
    company_representative: Optional[str] = Field(default=None, max_length=200)
    contact_email: Optional[str] = Field(default=None, max_length=200)
    privacy_email: Optional[str] = Field(default=None, max_length=200)
    register_info: Optional[str] = Field(default=None, max_length=400)
    vat_id: Optional[str] = Field(default=None, max_length=80)
    supervisory_authority: Optional[str] = Field(default=None, max_length=400)


class BrandingResponse(BaseModel):
    app_name: str
    sub_brand: str
    accent_color: str
    tagline: str
    logo_data_uri: str
    company_legal_name: str
    company_address: str
    company_representative: str
    contact_email: str
    privacy_email: str
    register_info: str
    vat_id: str
    supervisory_authority: str


# ---- Documents -----------------------------------------------------------

@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(_: dict = Depends(require_admin)) -> list[DocumentOut]:
    """List the *current* version of each document. Archived versions are
    counted in archived_versions so admins can see at a glance whether a
    doc has history. To inspect archived rows directly, query the
    documents table — there is no archive-listing endpoint by design
    (the UI doesn't surface old versions, only the current one)."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT d.id, d.source, d.ingested_at, d.version, d.tags, "
                "       (SELECT count(*) FROM chunks c WHERE c.document_id = d.id) AS chunks, "
                "       (SELECT count(*) FROM documents prior "
                "          WHERE prior.source = d.source AND prior.is_current = FALSE) AS archived_versions "
                "FROM documents d "
                "WHERE d.is_current = TRUE "
                "ORDER BY d.ingested_at DESC"
            )
            rows = await cur.fetchall()
    return [
        DocumentOut(
            id=r["id"],
            source=r["source"],
            ingested_at=r["ingested_at"].isoformat(),
            chunks=r["chunks"],
            version=r["version"],
            archived_versions=r["archived_versions"],
            tags=list(r["tags"] or []),
        )
        for r in rows
    ]


@router.post("/documents", status_code=201)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    # JSON-encoded list of tag strings, posted as a multipart form field
    # alongside the file. Empty/missing -> the document is public OR (when
    # this is a new version of an existing source) inherits the previous
    # version's tags. Inheritance avoids the failure mode where an admin
    # re-uploads to fix a typo and accidentally drops the ACL.
    tags: Optional[str] = Form(default=None),
    admin: dict = Depends(require_admin),
) -> dict:
    """Accept a single document, ingest it inline, and version-chain over
    any existing document with the same source name.

    Re-uploading a file with the same name no longer rejects — the prior
    current version is archived (is_current=FALSE) and the new content
    becomes version+1. The old bytes are preserved on disk as
    `<name>.v{prev_version}.bak` for restore / audit. Re-uploading bytes
    that are identical to any existing version (current or archived)
    still returns 409 since there's no semantic 'version' of identical
    content.
    """
    # Parse and validate the optional tags field. JSON-in-form-field is
    # the simplest way to send a list through multipart without dragging
    # in a separate JSON body.
    requested_tags: Optional[list[str]] = None
    if tags is not None and tags.strip() not in ("", "[]"):
        try:
            parsed = json.loads(tags)
        except ValueError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"tags: invalid JSON ({e})"
            ) from e
        if not isinstance(parsed, list):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "tags: must be a JSON array")
        try:
            requested_tags = normalise_tags(parsed)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if requested_tags is not None:
        # An explicit (possibly empty) list was supplied — validate against
        # the departments vocabulary. None means "inherit from previous
        # version", which can't be unknown by definition.
        try:
            await assert_known_departments(requested_tags)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    raw_name = (file.filename or "").strip()
    if not raw_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "filename required")

    name = Path(raw_name).name  # strip any directory components
    if not _SAFE_NAME.match(name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "filename must start with letter/digit and contain only letters, digits, dot, dash, underscore",
        )
    if _BACKUP_SUFFIX_RE.search(name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "reserved suffix .v<n>.bak — used for archived versions",
        )
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unsupported type {suffix!r}; allowed: {sorted(SUPPORTED_SUFFIXES)}",
        )

    # Read with a hard cap. Don't trust UploadFile.size — it can be None
    # depending on the client. Stream into memory with our own counter.
    payload = b""
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        payload += chunk
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"file exceeds {MAX_UPLOAD_BYTES // (1024*1024)}MB",
            )
    if not payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")

    file_hash = hashlib.sha256(payload).hexdigest()
    if await _document_already_ingested(file_hash):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this content is already ingested (possibly under a different filename or as an older version)",
        )

    # Optional malware scan (clamav profile). Fail-closed: if clamd is
    # configured but unreachable, the upload is rejected with 503.
    if get_settings().clamav_enabled:
        try:
            virus = await _clamd_scan(payload)
        except RuntimeError as e:
            log.error("clamav scan failed: %s", e)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "virus scanner unavailable; upload rejected",
            ) from e
        if virus is not None:
            log.warning(
                "admin upload virus_blocked user_id=%s name=%s signature=%s",
                admin["id"], name, virus,
            )
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"upload rejected: malware signature detected ({virus})",
            )

    log.info("admin upload user_id=%s name=%s bytes=%d", admin["id"], name, len(payload))

    # Run the shared per-item pipeline (parse -> normalize -> chunk -> embed
    # -> atomic versioned insert). Parsing and embedding happen off the event
    # loop inside ingest_item. tags=None means "inherit from previous version
    # if any, else public"; an explicit [] from the admin form means "make
    # this public regardless". Content-type is left empty so parse_bytes
    # dispatches on the (already-validated) filename suffix, exactly as the
    # prior in-handler parse did.
    #
    # Error mapping reproduces the prior responses:
    #   * parse failure / cap breach -> 400 "failed to parse: ..."
    #   * empty document             -> 400 "document produced no text"
    #   * None (dedup race)          -> 409
    # The cheap dedup pre-check inside ingest_item is redundant with the
    # explicit 409 check above but harmless (one extra cheap SELECT).
    item = SourceItem(source=name, raw_bytes=payload, content_type="")
    try:
        result = await ingest_item(
            item, _get_embedder(), tags=requested_tags, origin="local",
        )
    except EmptyDocumentError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "document produced no text"
        ) from e
    except ParseError as e:
        # Parse-layer failure only (ingest_item normalises malformed-PDF /
        # cap-breach / decode errors to ParseError). DB errors from the
        # insert step are deliberately NOT caught here — they propagate as
        # 500, exactly as before the refactor.
        log.exception("admin upload parse failed")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"failed to parse: {e}") from e

    if result is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "document raced with another upload")
    doc_id, n_inserted, version, prev_version = result

    # Disk handling — only after DB success, so we never leave bytes that
    # don't correspond to a row. If a current file with this name exists
    # on disk (from the previous version), preserve it as a .bak.
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    target = DOCS_DIR / name
    try:
        if target.exists() and prev_version is not None:
            backup = DOCS_DIR / f"{name}.v{prev_version}.bak"
            # If a backup already exists from an even older version, leave
            # it alone — overwriting destroys history.
            if not backup.exists():
                target.rename(backup)
            else:
                # Edge case: backup name already taken (rare). Fall through
                # and let target.write_bytes overwrite the current file.
                # The previous-version bytes on disk are lost; the row is
                # still in the DB for audit.
                log.warning("backup file %s already exists; new upload will overwrite", backup.name)
        target.write_bytes(payload)
    except OSError as e:
        log.warning("disk handling for %s failed (DB insert succeeded): %s", name, e)

    await _audit(
        admin["id"], "doc.upload", "document", doc_id,
        {
            "source": name, "bytes": len(payload), "chunks": n_inserted,
            "version": version, "previous_version": prev_version,
            # Either the explicit list the admin sent or "inherit" so the
            # audit row is unambiguous about how the ACL was decided.
            "tags": requested_tags if requested_tags is not None else "inherited",
        },
        request,
    )
    return {
        "id": doc_id, "source": name, "chunks": n_inserted,
        "version": version, "previous_version": prev_version,
    }


@router.post("/sources/sync")
async def sync_source(
    request: Request,
    body: SourceSyncRequest = Body(...),
    admin: dict = Depends(require_admin),
) -> dict:
    """Fetch an allow-list of URLs through the URL connector and ingest each
    into the document store with origin='url'.

    Security:
      * tags are validated against the departments vocabulary (the 5th
        controlled tag-write surface) before any fetch — external sources
        require an explicit ACL decision and are never auto-public;
      * each fetched payload is run through clamd (fail-closed, exactly like
        the upload path) BEFORE it is parsed;
      * the connector itself enforces the SSRF guard, scheme allow-list,
        byte cap (while streaming) and timeouts (see connectors/url.py).

    Per-item results are aggregated (ingested / skipped-duplicate / error) so
    one bad URL doesn't abort the whole run. A successful ingest emits a
    'source.sync' audit row per document.
    """
    # External sources require an EXPLICIT ACL decision. Unlike the upload
    # path (where tags=None inherits from the previous version, falling back
    # to public for a brand-new doc), a fetched URL must never silently become
    # public — so reject an omitted tags field outright. Pass [] to make the
    # documents public on purpose.
    if body.tags is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "external sources require an explicit 'tags' decision; "
            "use [] to deliberately make the documents public",
        )
    # body.tags is already normalised by the model validator; validate it
    # against the controlled departments vocabulary (the 5th tag-write surface).
    try:
        await assert_known_departments(body.tags)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    clamav_enabled = get_settings().clamav_enabled
    embedder = _get_embedder()

    results: list[dict] = []
    ingested = skipped = errors = 0

    # Drive each URL through its OWN connector instance so a fetch failure on
    # one URL (a blocked/oversize/unfetchable target — connectors.url.FetchError,
    # a RuntimeError subclass) is isolated and reported per item rather than
    # aborting the whole batch. The connector still runs the SSRF guard /
    # scheme allow-list / streaming byte cap / timeout on every hop.
    for raw_url in body.urls:
        connector = connector_factory({"type": body.type, "urls": [raw_url]})
        try:
            item = await connector.iter_items().__anext__()
        except StopAsyncIteration:
            # Connector yielded nothing for this URL (shouldn't happen for a
            # successful single-URL fetch, but treat as a skip rather than a
            # crash).
            skipped += 1
            results.append({"url": raw_url, "status": "skipped",
                            "reason": "no content"})
            continue
        except Exception as e:  # FetchError / SSRF reject / transport error
            errors += 1
            log.warning("source.sync fetch failed for %s: %s", raw_url, e)
            results.append({"url": raw_url, "status": "error", "error": str(e)})
            continue

        url = item.source
        # Malware scan the fetched payload, fail-closed, before parsing —
        # same posture as the upload endpoint.
        if clamav_enabled:
            try:
                virus = await _clamd_scan(item.raw_bytes)
            except RuntimeError as e:
                log.error("source.sync clamav scan failed for %s: %s", url, e)
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "virus scanner unavailable; sync aborted",
                ) from e
            if virus is not None:
                errors += 1
                log.warning(
                    "source.sync virus_blocked user_id=%s url=%s signature=%s",
                    admin["id"], url, virus,
                )
                results.append({"url": url, "status": "error",
                                "error": f"malware signature detected ({virus})"})
                continue

        try:
            result = await ingest_item(
                item, embedder, tags=body.tags, origin="url",
            )
        except EmptyDocumentError:
            skipped += 1
            results.append({"url": url, "status": "skipped",
                            "reason": "produced no text"})
            continue
        except ParseError as e:
            errors += 1
            log.warning("source.sync parse failed for %s: %s", url, e)
            results.append({"url": url, "status": "error",
                            "error": f"failed to parse: {e}"})
            continue

        if result is None:
            skipped += 1
            results.append({"url": url, "status": "skipped",
                            "reason": "already ingested"})
            continue

        doc_id, n_inserted, version, prev_version = result
        ingested += 1
        results.append({
            "url": url, "status": "ingested", "id": doc_id,
            "chunks": n_inserted, "version": version,
            "previous_version": prev_version,
        })
        await _audit(
            admin["id"], "source.sync", "document", doc_id,
            {
                "origin": "url", "url": url, "bytes": len(item.raw_bytes),
                "chunks": n_inserted, "version": version,
                "tags": body.tags if body.tags is not None else "inherited",
            },
            request,
        )

    log.info(
        "admin source.sync actor=%s type=%s urls=%d ingested=%d skipped=%d errors=%d",
        admin["id"], body.type, len(body.urls), ingested, skipped, errors,
    )
    return {
        "ingested": ingested, "skipped": skipped, "errors": errors,
        "results": results,
    }


@router.patch("/documents/{document_id}/tags", response_model=DocumentOut)
async def update_document_tags(
    document_id: int,
    request: Request,
    body: UpdateDocumentTagsRequest = Body(...),
    admin: dict = Depends(require_admin),
) -> DocumentOut:
    """Replace a document's tags wholesale.

    Propagates the new tags to **every version** of the same source
    (current and archived). The previous behaviour — only updating the
    current row — was an information-disclosure bug: an admin could
    upload a doc as public, later restrict it, and the (still-public)
    archived version would remain reachable through GET /api/documents/{id}
    by enumerating BIGSERIAL ids (audit H-2, 2026-05-10).

    The trade-off is that "tags as a snapshot of past truth" is gone:
    if you later need to look at what an old answer was based on, the
    archived row's ACL reflects the document's *current* policy, not
    the policy at the time it was answered. That's the right default
    for a security boundary — a leak we can't audit beats a snapshot
    we can.
    """
    # Reject any tag that isn't in the departments vocabulary.
    # Migration 012 made the tag list a controlled vocabulary so the UI
    # can show a multi-select picker instead of a free comma-input.
    try:
        await assert_known_departments(body.tags)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    async with connection() as conn:
        async with conn.cursor() as cur:
            # Resolve the source from the current version first so we can
            # update every version under that source atomically. 404s if
            # the id doesn't point at a current row — archived ids are not
            # addressable here (admin should reach for the current one).
            await cur.execute(
                "SELECT source FROM documents WHERE id = %s AND is_current = TRUE",
                (document_id,),
            )
            current = await cur.fetchone()
            if current is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "document not found or not the current version",
                )
            source = current["source"]
            # One UPDATE across every version row. Archived rows are
            # unreachable from /api/chat (retrieve filters is_current=TRUE)
            # and from list_documents_public; the relevant surface is
            # main.get_document, which enumerates by id.
            await cur.execute(
                "UPDATE documents SET tags = %s WHERE source = %s",
                (body.tags, source),
            )
            # Fetch the updated current row for the response.
            await cur.execute(
                "SELECT d.id, d.source, d.ingested_at, d.version, d.tags, "
                "  (SELECT count(*) FROM chunks c WHERE c.document_id = d.id) AS chunks, "
                "  (SELECT count(*) FROM documents prior "
                "     WHERE prior.source = d.source AND prior.is_current = FALSE) AS archived_versions "
                "FROM documents d WHERE d.id = %s",
                (document_id,),
            )
            row = await cur.fetchone()
        await conn.commit()
    log.info("admin doc-tags actor=%s doc=%s tags=%s",
             admin["id"], document_id, body.tags)
    await _audit(
        admin["id"], "doc.tags", "document", document_id,
        {"tags": body.tags, "source": row["source"]},
        request,
    )
    return DocumentOut(
        id=row["id"],
        source=row["source"],
        ingested_at=row["ingested_at"].isoformat(),
        chunks=row["chunks"],
        version=row["version"],
        archived_versions=row["archived_versions"],
        tags=list(row["tags"] or []),
    )


@router.get("/tags")
async def list_known_tags(_: dict = Depends(require_admin)) -> dict:
    """Legacy: returns distinct tags currently in use across documents
    and users. Superseded by /api/admin/departments — kept so older
    clients (and the Tag-bearbeiten autocomplete) still work."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT t FROM ("
                "  SELECT unnest(tags) AS t FROM documents "
                "  UNION ALL "
                "  SELECT unnest(allowed_tags) AS t FROM users "
                ") sub WHERE t IS NOT NULL ORDER BY t"
            )
            rows = await cur.fetchall()
    return {"tags": [r["t"] for r in rows]}


# ---- Departments (controlled vocabulary for tags) -----------------------

class DepartmentOut(BaseModel):
    name: str
    label: str
    created_at: str


class CreateDepartmentRequest(BaseModel):
    """A department's `name` is the slug stored in tags arrays; `label`
    is the display string shown in the picker. Slug grammar matches
    acls.normalise_tag (lowercase ascii, max 32 chars)."""
    name: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _norm_name(cls, v: str) -> str:
        # normalise_tag enforces the slug grammar (raises ValueError on bad input).
        from acls import normalise_tag
        return normalise_tag(v)

    @field_validator("label")
    @classmethod
    def _trim_label(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("label cannot be empty after trimming")
        return v


@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(_: dict = Depends(require_admin)) -> list[DepartmentOut]:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, label, created_at FROM departments ORDER BY label"
            )
            rows = await cur.fetchall()
    return [
        DepartmentOut(
            name=r["name"], label=r["label"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.post("/departments", response_model=DepartmentOut, status_code=201)
async def create_department(
    request: Request,
    body: CreateDepartmentRequest = Body(...),
    admin: dict = Depends(require_admin),
) -> DepartmentOut:
    async with connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO departments(name, label, created_by) "
                    "VALUES (%s, %s, %s) RETURNING name, label, created_at",
                    (body.name, body.label, admin["id"]),
                )
                row = await cur.fetchone()
            except Exception as e:
                if "departments_pkey" in str(e):
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        f"department '{body.name}' already exists",
                    ) from e
                raise
        await conn.commit()
    log.info("admin dept-create actor=%s name=%s", admin["id"], body.name)
    await _audit(
        admin["id"], "dept.create", "department", None,
        {"name": body.name, "label": body.label},
        request,
    )
    return DepartmentOut(
        name=row["name"], label=row["label"],
        created_at=row["created_at"].isoformat(),
    )


@router.delete("/departments/{name}", status_code=204)
async def delete_department(
    name: str,
    request: Request,
    admin: dict = Depends(require_admin),
) -> None:
    """Delete a department from the vocabulary AND strip it from every
    document.tags / users.allowed_tags array that referenced it. Without
    the strip the tag would orphan: still on rows, no longer pickable in
    the UI, and assert_known_departments would later refuse to save the
    same row unchanged."""
    from acls import normalise_tag
    try:
        canon = normalise_tag(name)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM departments WHERE name = %s RETURNING label",
                (canon,),
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")
            # array_remove is the postgres-native way to drop one element
            # from a TEXT[] in place.
            await cur.execute(
                "UPDATE documents SET tags = array_remove(tags, %s) "
                "WHERE %s = ANY(tags)", (canon, canon),
            )
            doc_count = cur.rowcount
            await cur.execute(
                "UPDATE users SET allowed_tags = array_remove(allowed_tags, %s) "
                "WHERE %s = ANY(allowed_tags)", (canon, canon),
            )
            user_count = cur.rowcount
        await conn.commit()
    log.info(
        "admin dept-delete actor=%s name=%s docs_stripped=%d users_stripped=%d",
        admin["id"], canon, doc_count, user_count,
    )
    await _audit(
        admin["id"], "dept.delete", "department", None,
        {
            "name": canon, "label": row["label"],
            "docs_stripped": doc_count, "users_stripped": user_count,
        },
        request,
    )


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: int,
    request: Request,
    admin: dict = Depends(require_admin),
) -> None:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM documents WHERE id = %s RETURNING source", (document_id,)
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    # Best-effort: also unlink the file from disk so a future ingest run
    # doesn't re-import the deleted document.
    try:
        (DOCS_DIR / row["source"]).unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not unlink %s: %s", row["source"], e)
    log.info("admin delete-doc user_id=%s id=%s source=%s",
             admin["id"], document_id, row["source"])
    await _audit(
        admin["id"], "doc.delete", "document", document_id,
        {"source": row["source"]},
        request,
    )


# ---- Users ---------------------------------------------------------------

def _row_to_user(r: dict) -> UserOut:
    return UserOut(
        id=r["id"],
        email=r["email"],
        is_active=bool(r["is_active"]),
        is_admin=bool(r["is_admin"]),
        created_at=r["created_at"].isoformat(),
        allowed_tags=list(r.get("allowed_tags") or []),
    )


# Shared SELECT list — every endpoint that returns a UserOut should use
# this to keep the columns in sync with _row_to_user.
_USER_COLS = "id, email, is_active, is_admin, created_at, allowed_tags"


@router.get("/users", response_model=list[UserOut])
async def list_users(_: dict = Depends(require_admin)) -> list[UserOut]:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_USER_COLS} FROM users ORDER BY created_at DESC"
            )
            rows = await cur.fetchall()
    return [_row_to_user(r) for r in rows]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    request: Request,
    body: CreateUserRequest = Body(...),
    admin: dict = Depends(require_admin),
) -> UserOut:
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid email")
    try:
        await assert_known_departments(body.allowed_tags)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    pw_hash = hash_password(body.password)
    async with connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO users(email, password_hash, is_admin, allowed_tags) "
                    "VALUES (%s, %s, %s, %s) "
                    f"RETURNING {_USER_COLS}",
                    (email, pw_hash, body.is_admin, body.allowed_tags),
                )
                row = await cur.fetchone()
            except Exception as e:
                # Race or duplicate email — surface as 409, not 500.
                if "users_email_key" in str(e):
                    raise HTTPException(status.HTTP_409_CONFLICT, "email already exists") from e
                raise
        await conn.commit()
    log.info("admin create-user actor=%s email=%s admin=%s tags=%s",
             admin["id"], email, body.is_admin, body.allowed_tags)
    await _audit(
        admin["id"], "user.create", "user", row["id"],
        {
            "email": email,
            "is_admin": bool(body.is_admin),
            "allowed_tags": body.allowed_tags,
        },
        request,
    )
    return _row_to_user(row)


@router.post("/users/{user_id}/reset-password", status_code=204)
async def reset_password(
    user_id: int,
    request: Request,
    body: ResetPasswordRequest = Body(...),
    admin: dict = Depends(require_admin),
) -> None:
    pw_hash = hash_password(body.password)
    async with connection() as conn:
        async with conn.cursor() as cur:
            # Admin reset is also the recovery path for lost 2FA devices —
            # clearing totp here lets the user back in with the new password
            # without needing a working authenticator. They can re-enable
            # 2FA from their account afterwards.
            await cur.execute(
                "UPDATE users SET password_hash = %s, "
                "  failed_login_count = 0, locked_until = NULL, "
                "  totp_secret = NULL, totp_enabled = FALSE "
                "WHERE id = %s",
                (pw_hash, user_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
            # Invalidate all existing sessions and login challenges for this
            # user — anyone holding a valid cookie or a stale TOTP challenge
            # before the reset must re-authenticate.
            await cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
            await cur.execute("DELETE FROM login_challenges WHERE user_id = %s", (user_id,))
        await conn.commit()
    log.info("admin reset-pw actor=%s target=%s", admin["id"], user_id)
    await _audit(
        admin["id"], "user.reset_password", "user", user_id,
        {},  # never log the password, hashed or otherwise
        request,
    )


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    request: Request,
    body: UpdateUserRequest = Body(...),
    admin: dict = Depends(require_admin),
) -> UserOut:
    # Self-protection: an admin cannot demote or deactivate themselves.
    # Avoids accidental lock-out from the admin UI.
    if user_id == admin["id"]:
        if body.is_admin is False:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot demote yourself")
        if body.is_active is False:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot deactivate yourself")

    sets = []
    params: list = []
    if body.is_admin is not None:
        sets.append("is_admin = %s")
        params.append(body.is_admin)
    if body.is_active is not None:
        sets.append("is_active = %s")
        params.append(body.is_active)
    if not sets:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no fields to update")
    params.append(user_id)
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = %s "  # noqa: S608 — column names are constants
                f"RETURNING {_USER_COLS}",
                params,
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
            # If we deactivated the user, kill their sessions too.
            if body.is_active is False:
                await cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        await conn.commit()
    log.info("admin update-user actor=%s target=%s admin=%s active=%s",
             admin["id"], user_id, body.is_admin, body.is_active)
    await _audit(
        admin["id"], "user.update", "user", user_id,
        {
            k: v for k, v in {
                "is_admin": body.is_admin,
                "is_active": body.is_active,
            }.items() if v is not None
        },
        request,
    )
    return _row_to_user(row)


@router.patch("/users/{user_id}/allowed-tags", response_model=UserOut)
async def update_user_allowed_tags(
    user_id: int,
    request: Request,
    body: UpdateUserTagsRequest = Body(...),
    admin: dict = Depends(require_admin),
) -> UserOut:
    """Replace a user's allowed_tags wholesale. No self-protection: an
    admin can change their own tags freely (admins bypass the ACL anyway,
    so this only matters if they later get demoted)."""
    try:
        await assert_known_departments(body.allowed_tags)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET allowed_tags = %s WHERE id = %s "
                f"RETURNING {_USER_COLS}",
                (body.allowed_tags, user_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
        await conn.commit()
    log.info("admin user-tags actor=%s target=%s tags=%s",
             admin["id"], user_id, body.allowed_tags)
    await _audit(
        admin["id"], "user.allowed_tags", "user", user_id,
        {"allowed_tags": body.allowed_tags},
        request,
    )
    return _row_to_user(row)


# ---- Analytics dashboard --------------------------------------------------

@router.get("/analytics")
async def analytics(_: dict = Depends(require_admin)) -> dict:
    """Aggregate metrics for the admin dashboard.

    All queries hit small tables (typically <100k rows for an internal
    deployment) and run in a single connection borrowed from the pool.
    For very large corpora these can be moved to materialised views.
    """
    REFUSAL_PREFIX = "Ich kann das anhand der verfügbaren Dokumente"

    async with connection() as conn:
        async with conn.cursor() as cur:
            # ---- Totals ----
            await cur.execute(
                "SELECT "
                "  (SELECT count(*) FROM users) AS users, "
                "  (SELECT count(*) FROM documents) AS documents, "
                "  (SELECT count(*) FROM conversations) AS conversations, "
                "  (SELECT count(*) FROM messages) AS messages"
            )
            totals = await cur.fetchone()

            # ---- Activity windows ----
            await cur.execute(
                "SELECT "
                "  (SELECT count(*) FROM conversations WHERE created_at >= now() - interval '7 days') AS conv_7d, "
                "  (SELECT count(*) FROM conversations WHERE created_at >= now() - interval '30 days') AS conv_30d, "
                "  (SELECT count(*) FROM messages      WHERE created_at >= now() - interval '7 days') AS msg_7d, "
                "  (SELECT count(*) FROM messages      WHERE created_at >= now() - interval '30 days') AS msg_30d, "
                "  (SELECT count(DISTINCT c.user_id) FROM conversations c "
                "     JOIN messages m ON m.conversation_id = c.id "
                "     WHERE m.created_at >= now() - interval '7 days') AS active_7d, "
                "  (SELECT count(DISTINCT c.user_id) FROM conversations c "
                "     JOIN messages m ON m.conversation_id = c.id "
                "     WHERE m.created_at >= now() - interval '30 days') AS active_30d"
            )
            recent = await cur.fetchone()

            # ---- Feedback distribution ----
            await cur.execute(
                "SELECT "
                "  (SELECT count(*) FROM message_feedback WHERE rating = 1) AS thumbs_up, "
                "  (SELECT count(*) FROM message_feedback WHERE rating = -1) AS thumbs_down, "
                "  (SELECT count(*) FROM message_feedback) AS rated, "
                "  (SELECT count(*) FROM message_feedback WHERE comment IS NOT NULL) AS with_comment, "
                "  (SELECT count(*) FROM messages WHERE role = 'assistant') AS assistant_total"
            )
            fb = await cur.fetchone()

            # ---- Refusal rate (last 30d) ----
            await cur.execute(
                "SELECT "
                "  count(*) FILTER (WHERE content LIKE %s)::float "
                "    / NULLIF(count(*), 0) AS rate "
                "FROM messages WHERE role = 'assistant' "
                "AND created_at >= now() - interval '30 days'",
                (REFUSAL_PREFIX + "%",),
            )
            refusal_row = await cur.fetchone()
            refusal_rate = float(refusal_row["rate"] or 0.0)

            # ---- Most-cited documents (last 30d). sources is a JSONB array
            # of {source, chunk_index, text}; jsonb_array_elements unnests.
            await cur.execute(
                "SELECT (s->>'source') AS source, count(*) AS n "
                "FROM messages m, jsonb_array_elements(m.sources) s "
                "WHERE m.role = 'assistant' "
                "AND m.created_at >= now() - interval '30 days' "
                "GROUP BY source ORDER BY n DESC LIMIT 10"
            )
            most_cited = [{"source": r["source"], "count": r["n"]} for r in await cur.fetchall()]

            # ---- Daily chat-call series (last 30d) from chat_usage ----
            await cur.execute(
                "SELECT day::text AS day, sum(request_count)::int AS count "
                "FROM chat_usage "
                "WHERE day >= CURRENT_DATE - interval '30 days' "
                "GROUP BY day ORDER BY day"
            )
            daily = [{"day": r["day"], "count": r["count"]} for r in await cur.fetchall()]

            # ---- Login attempts last 7d ----
            await cur.execute(
                "SELECT "
                "  count(*) FILTER (WHERE success = TRUE) AS success, "
                "  count(*) FILTER (WHERE success = FALSE) AS failure "
                "FROM login_attempts "
                "WHERE occurred_at >= now() - interval '7 days'"
            )
            la = await cur.fetchone()

    return {
        "totals": dict(totals),
        "recent": {
            "conversations_7d": recent["conv_7d"],
            "conversations_30d": recent["conv_30d"],
            "messages_7d": recent["msg_7d"],
            "messages_30d": recent["msg_30d"],
            "active_users_7d": recent["active_7d"],
            "active_users_30d": recent["active_30d"],
        },
        "feedback": {
            "thumbs_up": fb["thumbs_up"],
            "thumbs_down": fb["thumbs_down"],
            "rated": fb["rated"],
            "unrated_assistant_msgs": max(0, fb["assistant_total"] - fb["rated"]),
            "with_comment": fb["with_comment"],
        },
        "refusal_rate_30d": refusal_rate,
        "most_cited_30d": most_cited,
        "daily_chats_30d": daily,
        "login_attempts_7d": {
            "success": la["success"], "failure": la["failure"],
        },
    }


class EvalRetrieveRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class EvalHit(BaseModel):
    source: str
    chunk_index: int
    score: float
    distance: float


@router.post("/eval/retrieve", response_model=list[EvalHit])
@limiter.limit("30/minute")
async def eval_retrieve(
    request: Request,
    body: EvalRetrieveRequest = Body(...),
    admin: dict = Depends(require_admin),
) -> list[EvalHit]:
    """Retrieval-only endpoint for the eval CLI.

    Runs the same retrieve+rerank pipeline as /api/chat but skips the LLM
    call entirely. No conversation row is created and no budget counter is
    incremented — eval runs are otherwise indistinguishable from real
    user traffic, which would skew metrics and burn OpenRouter budget.

    Bypass-flag note: this endpoint deliberately calls retrieve() with
    `is_admin=True, allowed_tags=[]` so the eval CLI sees the full
    corpus. That bypass is forensically interesting (someone could
    exfiltrate restricted docs through it) — audit-logged with the
    query length but never the query content (A09) (audit C1-MED-3 +
    C5-LOW-1, 2026-05-12).
    """
    from rag import retrieve  # local import: avoids loading sentence-transformers at module init

    hits = await retrieve(
        body.message, is_admin=True, allowed_tags=[], history=None,
    )
    await _audit(
        admin["id"], "eval.retrieve", "query", None,
        {"query_len": len(body.message), "hits": len(hits)},
        request,
    )
    return [
        EvalHit(
            source=h.source,
            chunk_index=h.chunk_index,
            score=h.score,
            distance=h.distance,
        )
        for h in hits
    ]


@router.patch("/branding", response_model=BrandingResponse)
async def update_branding(
    request: Request,
    body: UpdateBrandingRequest = Body(...),
    admin: dict = Depends(require_admin),
) -> BrandingResponse:
    """Update the singleton branding row. Only fields explicitly provided
    are touched; unset fields keep their current value."""
    if body.accent_color is not None and not _HEX_COLOR_RE.match(body.accent_color):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "accent_color must be #RRGGBB or #RGB hex",
        )

    # Validate the logo data-URI strictly: prefix allowlist + byte cap. An
    # empty string is allowed and means "clear the custom logo" (the public
    # endpoint falls back to the neutral baked-in mark).
    logo_byte_len: Optional[int] = None
    if body.logo_data_uri is not None and body.logo_data_uri != "":
        uri = body.logo_data_uri
        if not uri.startswith(_LOGO_DATA_URI_PREFIXES):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "logo_data_uri must be a base64 data-URI of type "
                "image/svg+xml or image/png",
            )
        logo_byte_len = len(uri.encode("utf-8"))
        if logo_byte_len > _MAX_LOGO_DATA_URI_BYTES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"logo_data_uri exceeds {_MAX_LOGO_DATA_URI_BYTES // 1024} KB",
            )

    # Text fields that map 1:1 to a column and just get trimmed.
    _trimmed_cols = (
        "app_name", "sub_brand", "tagline",
        "company_legal_name", "company_address", "company_representative",
        "contact_email", "privacy_email", "register_info", "vat_id",
        "supervisory_authority",
    )
    sets = []
    params: list = []
    for col in _trimmed_cols:
        val = getattr(body, col)
        if val is not None:
            sets.append(f"{col} = %s"); params.append(val.strip())
    if body.accent_color is not None:
        sets.append("accent_color = %s"); params.append(body.accent_color)
    if body.logo_data_uri is not None:
        # Not trimmed — a data-URI has no meaningful surrounding whitespace
        # and trimming could corrupt it. Empty string clears it.
        sets.append("logo_data_uri = %s"); params.append(body.logo_data_uri)
    if not sets:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no fields to update")
    sets.append("updated_at = now()")
    _returning = (
        "app_name, sub_brand, accent_color, tagline, logo_data_uri, "
        "company_legal_name, company_address, company_representative, "
        "contact_email, privacy_email, register_info, vat_id, "
        "supervisory_authority"
    )
    sql = "UPDATE branding SET " + ", ".join(sets) + " WHERE id = 1 " \
          "RETURNING " + _returning
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))  # noqa: S608 — column list is constants
            row = await cur.fetchone()
        await conn.commit()
    log.info("admin update-branding actor=%s", admin["id"])
    # Audit the changed keys, but never the logo blob — record its size only.
    changed = body.model_dump(exclude_unset=True)
    audit_details: dict[str, Any] = {}
    for k, v in changed.items():
        if k == "logo_data_uri":
            audit_details[k] = (
                f"logo_data_uri changed, {logo_byte_len or 0} bytes"
            )
        else:
            audit_details[k] = v
    await _audit(
        admin["id"], "branding.update", "branding", 1,
        audit_details,
        request,
    )
    return BrandingResponse(**row)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    request: Request,
    admin: dict = Depends(require_admin),
) -> None:
    if user_id == admin["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot delete yourself")
    async with connection() as conn:
        async with conn.cursor() as cur:
            # Capture the email before delete so the audit row is meaningful
            # even after the FK target is gone (target_id will become a
            # dangling reference, which is fine — we keep the email in details).
            await cur.execute(
                "SELECT email FROM users WHERE id = %s", (user_id,),
            )
            target = await cur.fetchone()
            if target is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
            await cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        await conn.commit()
    log.info("admin delete-user actor=%s target=%s", admin["id"], user_id)
    await _audit(
        admin["id"], "user.delete", "user", user_id,
        {"email": target["email"]},
        request,
    )

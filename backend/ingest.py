"""Document ingestion CLI.

Usage (inside the backend container):
    python ingest.py /app/docs

Walks the directory, parses .pdf / .md / .txt, chunks at ~500 tokens with
50-token overlap, embeds locally via sentence-transformers, and writes to
Postgres.

Idempotency: each chunk's content is sha256-hashed; the chunks table has a
UNIQUE constraint on content_hash, and we use ON CONFLICT DO NOTHING. The
documents table has the same constraint on the *file* hash, so re-running on
unchanged input is a no-op. Editing a file produces new chunk hashes that
get inserted alongside the old ones; if you want a clean replace, delete the
documents row first (cascades to chunks).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
import sys
from pathlib import Path
from typing import Iterable

import numpy as np

from config import get_settings
from db import apply_migrations, close_pool, connection, init_pool

log = logging.getLogger("ingest")

SUPPORTED_SUFFIXES = {".pdf", ".md", ".markdown", ".txt"}

# Approximate token budget. We don't have the LLM's tokenizer here, and
# sentence-transformers' tokenizer is BERT-style. Word-count is a rough
# stand-in: bge-small's max input is 512 tokens (~380 words), so we target
# 500 tokens ≈ 380 words and overlap 50 tokens ≈ 38 words. Close enough;
# precise tokenization is overkill for retrieval-quality chunks.
CHUNK_WORDS = 380
OVERLAP_WORDS = 38

# Same caps as the admin upload path (admin.py:MAX_PDF_PAGES /
# MAX_PDF_TEXT_CHARS) so the offline CLI can't be tricked into OOMing
# the embedder via a decompression-bomb-style PDF either (audit M-4).
MAX_PDF_PAGES = 500
MAX_PDF_TEXT_CHARS = 5_000_000


# ---- Parsing -------------------------------------------------------------

def _read_pdf_bytes(name: str, raw: bytes) -> str:
    # Lazy import: pypdf is heavy and unused for plain-text paths.
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(raw))
    if len(reader.pages) > MAX_PDF_PAGES:
        # CLI surfaces this as a hard fail (operator sees the error in
        # ingest output); the admin endpoint wraps it as 400.
        raise ValueError(f"{name}: exceeds {MAX_PDF_PAGES} pages")
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:
            # Some PDFs have malformed pages. Skip them rather than aborting
            # the whole document.
            log.warning("pypdf failed to extract a page from %s", name)
            txt = ""
        total += len(txt)
        if total > MAX_PDF_TEXT_CHARS:
            raise ValueError(
                f"{name}: extracted text exceeds {MAX_PDF_TEXT_CHARS // 1_000_000} MB"
            )
        parts.append(txt)
    return "\n\n".join(parts)


def _is_pdf(name: str, content_type: str) -> bool:
    """Dispatch helper shared by parse_bytes. PDF iff the content-type says
    so or the suffix is .pdf. Everything else falls through to utf-8 decode,
    which mirrors the prior admin._parse_payload behaviour (it treated any
    non-.pdf upload as text)."""
    if (content_type or "").split(";", 1)[0].strip().lower() == "application/pdf":
        return True
    return Path(name).suffix.lower() == ".pdf"


def parse_bytes(name: str, raw: bytes, content_type: str = "") -> str:
    """Parse raw bytes into text by content-type / suffix dispatch.

    Shared by the offline CLI (via parse_file) and the in-app upload
    endpoint (admin.upload_document). PDFs are capped at MAX_PDF_PAGES /
    MAX_PDF_TEXT_CHARS; everything else is decoded as utf-8 with
    error-replacement. Cap breaches raise ValueError — callers map that to
    a hard CLI failure or a 400, respectively.
    """
    if _is_pdf(name, content_type):
        return _read_pdf_bytes(name, raw)
    return raw.decode("utf-8", errors="replace")


def parse_file(path: Path) -> str:
    """CLI helper: read a file off disk and parse it. Dispatch is by suffix
    (no content-type available locally). The suffix allow-list lives in
    iter_files / SUPPORTED_SUFFIXES; this still rejects unknown suffixes so
    a directly-passed file path can't slip through."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_bytes(path.name, path.read_bytes(), "application/pdf")
    if suffix in {".md", ".markdown", ".txt"}:
        return parse_bytes(path.name, path.read_bytes(), "text/plain")
    raise ValueError(f"unsupported file type: {suffix}")


# ---- Chunking ------------------------------------------------------------

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    # Collapse runs of whitespace, drop leading/trailing space, but keep
    # paragraph boundaries intact (double-newline).
    paragraphs = [_WS.sub(" ", p).strip() for p in text.split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p)


def chunk_text(text: str, size: int = CHUNK_WORDS, overlap: int = OVERLAP_WORDS) -> list[str]:
    """Sliding window over whitespace-split tokens."""
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("invalid chunk size/overlap")
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    step = size - overlap
    for start in range(0, len(words), step):
        window = words[start : start + size]
        if not window:
            break
        chunks.append(" ".join(window))
        if start + size >= len(words):
            break
    return chunks


# ---- Hashing -------------------------------------------------------------

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ---- Embedding -----------------------------------------------------------

class Embedder:
    """Lazy wrapper so importing this module doesn't load a 130MB model."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        # Imported here so the CLI startup is fast for --help etc.
        from sentence_transformers import SentenceTransformer

        log.info("loading embedding model %s", self._model_name)
        self._model = SentenceTransformer(self._model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        self._load()
        # multilingual-e5 expects "passage: " on document text. Without the
        # prefix the model still returns a vector but retrieval quality
        # drops noticeably — query/passage embeddings live in subtly
        # different sub-spaces. Queries are prefixed with "query: " in
        # rag.embed_query.
        # We normalise so cosine == dot product, which makes pgvector's
        # cosine-distance op-class agree with how we trained.
        assert self._model is not None
        prefixed = [f"passage: {t}" for t in texts]
        embs = self._model.encode(
            prefixed,
            batch_size=32,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return embs.astype(np.float32, copy=False)


# ---- Source items --------------------------------------------------------

class ParseError(ValueError):
    """Raised by ingest_item when parsing the raw bytes fails (a malformed
    PDF, a page/char cap breach, etc.).

    Subclasses ValueError. The admin upload path catches this to reproduce
    its prior 400 "failed to parse: ..." response *without* also swallowing
    DB-layer errors from the insert step (which must still surface as 500,
    as they did before this refactor). The CLI lets it propagate and abort
    the run, matching the prior parse_file behaviour."""


class EmptyDocumentError(ValueError):
    """Raised by ingest_item when a parsed document yields no chunks.

    Subclasses ValueError so callers that only care about "this item could
    not be turned into a document" can catch ValueError, while the admin
    upload path can catch this specifically to reproduce its prior 400
    "document produced no text" response (distinct from the dedup/race
    None return)."""


# SourceItem moved to the connectors package in Phase 3; re-exported here so
# existing call sites (`from ingest import SourceItem`) and tests keep working.
from connectors import SourceItem  # noqa: E402,F401  (re-export for back-compat)


# ---- Insertion -----------------------------------------------------------

async def _document_already_ingested(file_hash: str) -> bool:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM documents WHERE content_hash = %s", (file_hash,)
            )
            return await cur.fetchone() is not None


async def _insert_document_with_chunks(
    file_hash: str,
    source: str,
    chunks: list[str],
    hashes: list[str],
    embeddings: np.ndarray,
    tags: list[str] | None = None,
    origin: str = "local",
    source_type: str = "file",
    source_meta: dict | None = None,
) -> tuple[int, int, int, int | None] | None:
    """Insert a document and its chunks atomically.

    If a document with the same source name is currently is_current=TRUE,
    it is archived (is_current=FALSE, superseded_at=now()) and the new
    document inherits parent_id + version+1. Same content (hash collision)
    rolls back so the prior current stays current — there's no semantic
    "version" of identical bytes.

    Tags handling:
      * tags=None and no prev -> public ('{}')
      * tags=None and prev    -> inherit prev.tags (so re-uploading a
                                   doc to fix a typo doesn't accidentally
                                   strip the ACL)
      * tags=[...]            -> use as-is, including [] to make public

    ``source_type``/``source_meta`` (migration 016) record the ingest
    channel ('file' for the local/URL/upload paths, 'crm' for the CRM sync)
    and a small JSONB metadata blob (e.g. ``{"connector": "sql"}``). They
    are metadata only — they do NOT participate in dedup or the ACL — and
    default to 'file'/{} so existing callers are unchanged.

    Returns (document_id, inserted_chunks, version, prev_version) on
    success, or None if the content was already ingested.
    """
    from psycopg.types.json import Json

    meta = source_meta if source_meta is not None else {}
    async with connection() as conn:
        try:
            async with conn.cursor() as cur:
                # Archive previous current with the same source name (if any).
                # Same transaction as the insert below, so if the insert
                # rolls back, this UPDATE rolls back too.
                await cur.execute(
                    "UPDATE documents SET is_current = FALSE, superseded_at = now() "
                    "WHERE source = %s AND is_current = TRUE "
                    "RETURNING id, version, tags",
                    (source,),
                )
                prev = await cur.fetchone()
                parent_id = prev["id"] if prev else None
                version = (prev["version"] + 1) if prev else 1

                if tags is not None:
                    resolved_tags = tags
                elif prev is not None:
                    # Inherit from the version we just archived.
                    resolved_tags = list(prev["tags"] or [])
                else:
                    resolved_tags = []

                await cur.execute(
                    "INSERT INTO documents(source, content_hash, parent_id, version, tags, origin, source_type, source_meta) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (content_hash) DO NOTHING RETURNING id",
                    (source, file_hash, parent_id, version, resolved_tags, origin, source_type, Json(meta)),
                )
                row = await cur.fetchone()
                if row is None:
                    # content_hash collision — same bytes already in DB,
                    # possibly under a different name or as an older version.
                    # Rollback restores the prev row's is_current state.
                    await conn.rollback()
                    return None
                document_id = row["id"]
                inserted = 0
                for idx, (text, h, vec) in enumerate(zip(chunks, hashes, embeddings)):
                    await cur.execute(
                        "INSERT INTO chunks(document_id, chunk_index, content, "
                        "content_hash, embedding) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (content_hash) DO NOTHING",
                        (document_id, idx, text, h, vec),
                    )
                    if cur.rowcount > 0:
                        inserted += 1
            await conn.commit()
            return document_id, inserted, version, (prev["version"] if prev else None)
        except Exception:
            await conn.rollback()
            raise


# ---- Shared per-item pipeline --------------------------------------------

async def ingest_item(
    item: SourceItem,
    embedder: Embedder,
    *,
    tags: list[str] | None,
    origin: str = "local",
    source_type: str = "file",
    source_meta: dict | None = None,
) -> tuple[int, int, int, int | None] | None:
    """Run the full per-document pipeline for one SourceItem.

    dedup pre-check -> parse -> normalize -> chunk -> hash -> embed ->
    atomic insert/version. Shared by the offline CLI loop and the in-app
    admin upload endpoint so the two can never drift.

    The file hash is sha256 over the raw bytes (matching both the prior
    ``sha256_file`` CLI path and the admin handler's ``sha256(payload)``),
    so the content_hash dedup key is byte-for-byte identical to before.

    Returns the ``_insert_document_with_chunks`` tuple
    ``(document_id, inserted_chunks, version, prev_version)`` on success,
    or ``None`` when the content was already ingested (cheap pre-check or a
    content_hash collision in the atomic insert / race with another writer).
    Raises ``EmptyDocumentError`` if the parsed document yields no chunks —
    callers decide whether that's a skip (CLI) or a 400 (admin). Any
    parse-layer failure (malformed PDF, page/char cap breach, decode error)
    is normalised to ``ParseError`` (a ``ValueError`` subclass) so the admin
    path can map it to a 400 without also swallowing DB errors from the
    insert step.

    ``tags`` is forwarded verbatim to ``_insert_document_with_chunks``
    (None -> inherit/public, [] -> public, [...] -> set). ``origin`` records
    the provenance of the document ('local'/'file' for the filesystem walk,
    'url' for the URL connector) and is persisted on the ``documents`` row
    (migration 015) for audit/display only — it does not affect dedup or the
    ACL.
    """
    file_hash = hashlib.sha256(item.raw_bytes).hexdigest()
    # Cheap pre-check so we don't parse + embed a doc whose content_hash we
    # already have. The atomic insert below would also skip it, but only
    # after wasting work.
    if await _document_already_ingested(file_hash):
        return None

    # Parsing (pypdf) and embedding (sentence-transformers) are CPU-bound and
    # blocking; offload to a worker thread so this coroutine doesn't stall the
    # event loop when called from the FastAPI request path. In the CLI there's
    # no loop to protect, but to_thread is harmless there.
    try:
        raw = await asyncio.to_thread(
            parse_bytes, item.source, item.raw_bytes, item.content_type
        )
    except Exception as e:
        # Any parse-layer failure (malformed PDF, cap breach, decode error)
        # is normalised to ParseError so callers can distinguish it from a
        # later DB error. Keeps the original message text.
        raise ParseError(str(e) or e.__class__.__name__) from e
    text = normalize(raw)
    chunks = chunk_text(text)
    if not chunks:
        raise EmptyDocumentError(f"{item.source}: produced no chunks")

    hashes = [sha256_text(c) for c in chunks]
    embs = await asyncio.to_thread(embedder.encode, chunks)
    return await _insert_document_with_chunks(
        file_hash, item.source, chunks, hashes, embs, tags=tags, origin=origin,
        source_type=source_type, source_meta=source_meta,
    )


# ---- Driver --------------------------------------------------------------

def iter_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES:
            yield p


async def ingest_connector(spec: dict, *, tags: list[str] | None, origin: str) -> None:
    """Drive a connector (built from ``spec``) through the shared per-item
    pipeline. Each yielded ``SourceItem`` is parsed/chunked/embedded and
    version-inserted with the given ``tags`` and ``origin``.

    ``tags`` is the explicit ACL decision (validated by the caller for
    external sources): None -> inherit/public, [] -> public, [...] -> set.
    Parse failures (ParseError) propagate and abort the run, matching the
    prior CLI behaviour; an empty/already-ingested item is logged and skipped.
    """
    # Local import to avoid a circular import at module load (connectors
    # imports SourceItem from this module via the re-export).
    from connectors import connector_factory

    settings = get_settings()
    embedder = Embedder(settings.embedding_model)

    # Idempotent — safe to run from the CLI even after the API has migrated.
    await apply_migrations()
    await init_pool()
    try:
        connector = connector_factory(spec)
        seen = 0
        async for item in connector.iter_items():
            seen += 1
            try:
                result = await ingest_item(
                    item, embedder, tags=tags, origin=origin,
                )
            except EmptyDocumentError:
                # Parsed to nothing (e.g. an image-only PDF). Skip, as before.
                log.warning("%s produced no chunks; skipping", item.source)
                continue
            # Parse failures (ParseError, e.g. cap breaches) propagate and
            # abort the run, matching the prior parse_file CLI behaviour.
            if result is None:
                # Content already ingested (cheap pre-check or the atomic
                # insert's content_hash collision) or a race with another
                # writer. Idempotent — log and move on.
                log.info("skip %s (already ingested or raced)", item.source)
                continue
            doc_id, n, version, prev_version = result
            if prev_version is not None:
                log.info(
                    "ingested %s as v%d (archived previous v%d): %d chunks (doc_id=%d)",
                    item.source, version, prev_version, n, doc_id,
                )
            else:
                log.info("ingested %s: %d chunks (doc_id=%d)", item.source, n, doc_id)
        if seen == 0:
            log.warning("connector %r yielded no items", spec.get("type"))
    finally:
        await close_pool()


async def ingest_path(root: Path) -> None:
    """Back-compat entry point: ingest a local file/directory tree.

    Thin wrapper over ``ingest_connector`` with the local connector and
    origin='local'/tags=None (public-or-inherit), preserving the exact
    behaviour of ``python ingest.py /app/docs``."""
    await ingest_connector({"type": "local", "root": root}, tags=None, origin="local")


def _load_urls_file(path: Path) -> list[str]:
    """Read a newline-delimited URL list. Blank lines and #-comments are
    ignored so an operator can annotate the file."""
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        urls.append(s)
    return urls


async def _run_url_sync(urls: list[str], tags: list[str]) -> None:
    """Validate tags against the controlled vocabulary, then drive the URL
    connector with origin='url'. Tags are validated BEFORE any fetch so a
    typo'd department fails loudly without touching the network."""
    # Local imports: keep the acls/db dependency out of the import graph for
    # callers that only ever ingest local files.
    from acls import assert_known_departments, normalise_tags

    normalised = normalise_tags(tags)
    # apply_migrations()/init_pool() happen inside ingest_connector; the
    # validator needs a pool, so open one here for the check, then let
    # ingest_connector manage its own lifecycle. init_pool is idempotent.
    await apply_migrations()
    await init_pool()
    try:
        await assert_known_departments(normalised)
    finally:
        await close_pool()
    await ingest_connector(
        {"type": "url", "urls": urls}, tags=normalised, origin="url",
    )


def main() -> int:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Ingest documents into the RAG store.")
    parser.add_argument(
        "path", type=Path, nargs="?",
        help="File or directory to ingest (local source)",
    )
    parser.add_argument(
        "--source", choices=["local", "url"], default="local",
        help="Connector to drive (default: local)",
    )
    parser.add_argument(
        "--urls-file", type=Path,
        help="Newline-delimited file of http(s) URLs (--source url)",
    )
    parser.add_argument(
        "--tags",
        help="JSON array of department tags for the URL source, e.g. '[\"hr\"]'. "
             "External sources require an explicit tags decision.",
    )
    args = parser.parse_args()

    if args.source == "url":
        if not args.urls_file:
            log.error("--source url requires --urls-file")
            return 2
        if not args.urls_file.exists():
            log.error("urls-file does not exist: %s", args.urls_file)
            return 2
        if args.tags is None:
            log.error(
                "--source url requires --tags (a JSON array; use '[]' to make "
                "the documents public — there is no auto-tagging from URLs)"
            )
            return 2
        import json
        try:
            parsed_tags = json.loads(args.tags)
        except ValueError as e:
            log.error("--tags is not valid JSON: %s", e)
            return 2
        if not isinstance(parsed_tags, list):
            log.error("--tags must be a JSON array")
            return 2
        urls = _load_urls_file(args.urls_file)
        if not urls:
            log.error("urls-file %s contained no URLs", args.urls_file)
            return 2
        try:
            asyncio.run(_run_url_sync(urls, parsed_tags))
        except ValueError as e:
            # Unknown department / bad tag — surface as a CLI failure.
            log.error("%s", e)
            return 2
        return 0

    # Local source (default).
    if args.path is None:
        log.error("local source requires a path argument")
        return 2
    if not args.path.exists():
        log.error("path does not exist: %s", args.path)
        return 2

    asyncio.run(ingest_path(args.path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

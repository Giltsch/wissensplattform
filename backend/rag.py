"""Retrieval + generation.

Pipeline:
1. If conversation history is non-empty, an LLM call rewrites the latest user
   question into a standalone search query (resolves anaphora like "und das
   gleiche für …", "ja, insgesamt 25?"). On first turn this is skipped.
2. Embed the (rewritten) query with the multilingual-e5 model. E5 expects
   the literal prefix "query: " on queries and "passage: " on documents —
   getting this wrong silently degrades retrieval quality.
3. Pull `retrieve_k` candidates from pgvector by cosine distance (wide pool).
4. Rerank with a cross-encoder (`bge-reranker-v2-m3`) — sigmoid-normalise
   the logits so we can apply a relevance threshold.
5. Below threshold → return no hits, and the system prompt routes the LLM
   to the standard refusal. This prevents the bot from confidently citing
   irrelevant chunks (the failure mode that produced "35 € Tagespauschale"
   in answer to a vacation-days follow-up).
6. Stream chat completion from the configured LLM provider (OpenRouter or
   a local OpenAI-compatible server; see llm_provider.py). Conversation
   history is replayed as plain user/assistant turns; only the final user
   message carries the retrieved <context> block.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx
import numpy as np

from acls import visibility_clause
from config import get_settings
from db import connection
from llm_provider import provider_endpoint, provider_headers
from prompts import render as render_prompt
from telemetry import get_tracer

_tracer = get_tracer("rag")

log = logging.getLogger(__name__)

# All LLM prompts live as Jinja2 templates in backend/prompts/. The
# system prompt wraps retrieved context in <context>...</context> and
# tells the model to treat that block as untrusted data; user-message
# templates assemble the actual context block.


# ---- Retrieval -----------------------------------------------------------

@dataclass(slots=True)
class Hit:
    chunk_id: int
    document_id: int
    source: str
    chunk_index: int
    content: str
    distance: float  # cosine distance from the bi-encoder
    score: float     # sigmoid-normalised cross-encoder score, [0, 1]


# Module-level singletons. Each model is ~280–570 MB; we want exactly one
# in-process copy. Loaded lazily by `_get_*`, or eagerly via `warmup()` from
# the FastAPI lifespan so the first user doesn't pay the load cost.
_embedder = None
_reranker = None
_torch_configured = False


def _configure_torch_for_concurrency() -> None:
    """Pin torch to one thread per inference, so concurrent requests scale
    across cores instead of fighting for them.

    PyTorch's default intra-op thread count is the host's core count, which
    is optimal for a single inference but pathological under concurrency:
    two simultaneous rerank calls each try to use all cores and end up
    roughly halving each other's throughput. Setting num_threads=1 makes
    a single call slightly slower (~2.85 s → ~3.5 s on 4 cores) but allows
    `N` cores to handle `N` concurrent reranks in true parallel. For a
    workshop-scale burst of 10–20 users that's the difference between a
    14-second tail and a 60-second tail.

    Must run **before** any model is loaded — torch reads the values at
    operator-instantiation time. Idempotent so warmup + first lazy-load
    can both call it safely.
    """
    global _torch_configured
    if _torch_configured:
        return
    try:
        import torch  # noqa: WPS433 — torch is heavy, deferred until first need

        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
    except (ImportError, RuntimeError):
        # RuntimeError fires if interop threads were already set in this
        # process (torch forbids second set). Harmless — just means another
        # path got here first.
        pass
    _torch_configured = True


def _get_embedder():
    global _embedder
    if _embedder is None:
        _configure_torch_for_concurrency()
        from sentence_transformers import SentenceTransformer

        settings = get_settings()
        log.info("loading embedding model %s", settings.embedding_model)
        _embedder = SentenceTransformer(settings.embedding_model)
    return _embedder


def _get_reranker():
    global _reranker
    if _reranker is None:
        _configure_torch_for_concurrency()
        from sentence_transformers import CrossEncoder

        settings = get_settings()
        log.info("loading reranker model %s", settings.reranker_model)
        _reranker = CrossEncoder(settings.reranker_model)
    return _reranker


async def warmup() -> None:
    """Force-load both models on a worker thread.

    Called from the FastAPI lifespan so the cold-start cost (~10 s on first
    load, mostly safetensors deserialisation) is paid before any user
    request lands. Falls back to lazy loading if the warmup itself fails —
    serving with a slow first request beats refusing to boot.
    """
    import anyio

    def _load_both() -> None:
        _get_embedder()
        _get_reranker()

    try:
        await anyio.to_thread.run_sync(_load_both)
        log.info("model warmup complete")
    except Exception:
        log.exception("model warmup failed; will retry lazily on first request")


async def embed_query(text: str) -> np.ndarray:
    """Embed a single query. E5 expects the 'query: ' prefix; without it
    similarity scores are noticeably worse."""
    import anyio

    def _encode() -> np.ndarray:
        model = _get_embedder()
        emb = model.encode(
            [f"query: {text}"],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )[0]
        return emb.astype(np.float32, copy=False)

    with _tracer.start_as_current_span("rag.embed_query") as span:
        # Length only — never the query text itself (A09).
        span.set_attribute("query.len", len(text))
        return await anyio.to_thread.run_sync(_encode)


def _sigmoid(x: float) -> float:
    # Numerically stable sigmoid; reranker logits can be in roughly [-10, +10].
    if x >= 0:
        z = np.exp(-x)
        return float(1.0 / (1.0 + z))
    z = np.exp(x)
    return float(z / (1.0 + z))


async def _rerank(query: str, candidates: list[dict]) -> list[tuple[dict, float]]:
    """Return [(row, sigmoid_score)] sorted by score desc."""
    import anyio

    if not candidates:
        return []

    pairs = [(query, c["content"]) for c in candidates]

    def _predict() -> list[float]:
        import torch

        model = _get_reranker()
        # Since sentence-transformers 4, predict() applies Sigmoid by default
        # for single-label rerankers. Ask for raw logits explicitly so the
        # _sigmoid below runs exactly once.
        logits = model.predict(
            pairs, activation_fn=torch.nn.Identity(), show_progress_bar=False
        )
        return [float(x) for x in logits]

    with _tracer.start_as_current_span("rag.rerank") as span:
        span.set_attribute("rerank.candidates", len(candidates))
        logits = await anyio.to_thread.run_sync(_predict)
        scored = [(row, _sigmoid(lg)) for row, lg in zip(candidates, logits)]
        scored.sort(key=lambda t: t[1], reverse=True)
        if scored:
            span.set_attribute("rerank.top_score", scored[0][1])
        return scored


def _format_history_for_rewrite(history: list[dict]) -> str:
    # Plain-text rendering for the rewriter prompt. Keep tight to fit in the
    # 80-token output budget without truncating the actual question.
    lines = []
    for msg in history:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        prefix = "Benutzer" if role == "user" else "Assistent"
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines)


async def rewrite_query(history: list[dict], question: str) -> str:
    """LLM-rewrite the latest question to a standalone search query.

    Returns the original question unchanged if history is empty, the LLM
    call fails, or the rewrite comes back empty. Failure must never break
    retrieval — this is a quality enhancement, not a hard dependency.
    """
    settings = get_settings()
    if not history or not settings.query_rewrite_enabled:
        # No-op fast path: not even a span — it would dominate the trace
        # stream on first turns where we always skip the rewrite.
        return question

    return await _rewrite_query_impl(history, question, settings)


async def _rewrite_query_impl(history: list[dict], question: str, settings) -> str:
    """The actual rewrite call. Split out so we can wrap it in a single
    span without copy-pasting around three return paths."""
    with _tracer.start_as_current_span("rag.rewrite") as span:
        span.set_attribute("rewrite.history_turns", len(history))
        history_block = _format_history_for_rewrite(history)
        user_block = render_prompt(
            "rewrite_user.j2", history_block=history_block, question=question
        )
        headers = provider_headers(settings)
        url = provider_endpoint(settings)
        body = {
            "model": settings.openrouter_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": render_prompt("rewrite_de.j2")},
                {"role": "user", "content": user_block},
            ],
            "max_tokens": settings.query_rewrite_max_tokens,
            "temperature": 0.0,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
                r = await client.post(url, headers=headers, json=body)
                if r.status_code >= 400:
                    log.warning("query rewrite http %s; falling back to raw question", r.status_code)
                    span.set_attribute("rewrite.fallback", "http_error")
                    return question
                data = r.json()
            rewritten = (
                data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
            )
            if not rewritten:
                span.set_attribute("rewrite.fallback", "empty_response")
                return question
            # Strip wrapping quotes/whitespace the model sometimes adds despite the prompt.
            rewritten = rewritten.strip().strip('"').strip("'").strip()
            # Hard cap. If something goes wrong and the model returns the whole
            # history concatenated, we don't want a 4000-char "query".
            if len(rewritten) > 500:
                rewritten = rewritten[:500]
                span.set_attribute("rewrite.truncated", True)
            log.info("query rewrite ok orig_len=%d new_len=%d", len(question), len(rewritten))
            span.set_attribute("rewrite.new_len", len(rewritten))
            return rewritten or question
        except Exception:
            log.exception("query rewrite failed; falling back to raw question")
            span.set_attribute("rewrite.fallback", "exception")
            return question


async def retrieve(
    query: str,
    *,
    is_admin: bool,
    allowed_tags: list[str],
    history: Optional[list[dict]] = None,
) -> list[Hit]:
    """End-to-end retrieval: rewrite → hybrid search → rerank → threshold.

    Hybrid: a dense vector query and a German-stemmed BM25 query run in
    parallel, then their candidate sets are union-deduped by chunk_id and
    fed jointly to the cross-encoder reranker. The reranker resolves the
    final ordering — this gives us BM25's keyword recall (proper nouns,
    codes the embedder hasn't seen) AND dense semantic match without
    handwriting a fusion formula.

    ACL: ``is_admin`` and ``allowed_tags`` are required keyword args
    (no defaults) so callers cannot accidentally retrieve unfiltered
    results — that would be a data leak. Admins bypass the filter; non-
    admins see only documents whose tag set is empty (public) or
    overlaps with their allowed_tags.
    """
    settings = get_settings()

    with _tracer.start_as_current_span("rag.retrieve") as outer:
        outer.set_attribute("acl.is_admin", is_admin)
        outer.set_attribute("acl.allowed_tags_count", len(allowed_tags))
        outer.set_attribute("retrieve.k", settings.retrieve_k)
        outer.set_attribute("retrieve.history_turns", len(history or []))

        search_query = await rewrite_query(history or [], query)
        vec = await embed_query(search_query)

        # Both paths filter d.is_current = TRUE so only the latest version of
        # each document contributes. Older archived versions remain in the DB
        # for audit but never surface in answers. The ACL fragment is appended
        # to the same WHERE, with %s placeholders bound through `acl_params`.
        acl_sql, acl_params = visibility_clause(is_admin, allowed_tags, doc_alias="d")

        # Dense path: cosine distance, top retrieve_k.
        dense_sql = (
            "SELECT c.id AS chunk_id, c.document_id, d.source, c.chunk_index, "
            "       c.content, (c.embedding <=> %s) AS distance "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            f"WHERE d.is_current = TRUE AND {acl_sql} "
            "ORDER BY c.embedding <=> %s "
            "LIMIT %s"
        )
        # BM25 path: plainto_tsquery silently ignores operators in user input,
        # which makes it safe against tsquery-injection. ts_rank_cd weights
        # cover-density (closer matches rank higher).
        bm25_sql = (
            "SELECT c.id AS chunk_id, c.document_id, d.source, c.chunk_index, "
            "       c.content, NULL::float AS distance "
            "FROM chunks c JOIN documents d ON d.id = c.document_id, "
            "     plainto_tsquery('german', %s) AS q "
            f"WHERE d.is_current = TRUE AND c.content_tsv @@ q AND {acl_sql} "
            "ORDER BY ts_rank_cd(c.content_tsv, q) DESC "
            "LIMIT %s"
        )
        async with connection() as conn:
            async with conn.cursor() as cur:
                with _tracer.start_as_current_span("rag.retrieve.dense") as ds:
                    await cur.execute(dense_sql, (vec, *acl_params, vec, settings.retrieve_k))
                    dense_rows = await cur.fetchall()
                    ds.set_attribute("retrieve.dense.rows", len(dense_rows))
                with _tracer.start_as_current_span("rag.retrieve.bm25") as bs:
                    await cur.execute(
                        bm25_sql,
                        (search_query, *acl_params, settings.retrieve_k // 2),
                    )
                    bm25_rows = await cur.fetchall()
                    bs.set_attribute("retrieve.bm25.rows", len(bm25_rows))

        # Union by chunk_id; first occurrence wins (preserves dense's distance
        # for telemetry). Ordering doesn't matter — the reranker resorts.
        by_id: dict[int, dict] = {}
        for r in dense_rows:
            by_id[r["chunk_id"]] = r
        for r in bm25_rows:
            by_id.setdefault(r["chunk_id"], r)
        rows = list(by_id.values())
        log.info(
            "retrieve hybrid dense=%d bm25=%d unique=%d",
            len(dense_rows), len(bm25_rows), len(rows),
        )
        outer.set_attribute("retrieve.unique_candidates", len(rows))

        if not rows:
            outer.set_attribute("retrieve.empty", True)
            return []

        scored = await _rerank(search_query, rows)
        if not scored:
            return []

        top_score = scored[0][1]
        outer.set_attribute("retrieve.top_score", top_score)
        if top_score < settings.relevance_threshold:
            outer.set_attribute("retrieve.below_threshold", True)
            log.info(
                "retrieve below_threshold top=%.3f thr=%.3f",
                top_score, settings.relevance_threshold,
            )
            return []

        selected = scored[: settings.rerank_top_k]
        outer.set_attribute("retrieve.selected", len(selected))
        return [
            Hit(
                chunk_id=r["chunk_id"],
                document_id=r["document_id"],
                source=r["source"],
                chunk_index=r["chunk_index"],
                content=r["content"],
                # NULL when this candidate came in via the BM25 path only.
                distance=float(r["distance"]) if r["distance"] is not None else float("inf"),
                score=score,
            )
            for r, score in selected
        ]


# ---- Prompt assembly -----------------------------------------------------

# A literal `</context>` (or `<context>`, or `</prev_user_message>`, etc.)
# inside a chunk or a historical user message would let an attacker close
# the delimited block early and inject "new" instructions for the LLM.
# We strip-and-escape these markers, AND we wrap the real block in a
# nonced delimiter (see `build_user_prompt`) so even a missed escape
# can't be guessed (audit C5-HIGH-1 / C5-HIGH-2, 2026-05-12).
_DELIMITER_RE = re.compile(
    r"</?(?:context|prev_user_message)[^>]*>", re.IGNORECASE
)


def _strip_delimiters(text: str) -> str:
    """Neutralise any literal `<context>`/`</context>` (incl. nonced
    variants the attacker might guess) and the history wrappers, so a
    document or historical message cannot fake a delimiter."""
    return _DELIMITER_RE.sub(
        lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"),
        text,
    )


def build_user_prompt(question: str, hits: list[Hit]) -> str:
    # Per-request nonce. 16 hex chars (~64 bits) is unguessable for the
    # attacker who only sees their own uploaded document — they cannot
    # predict the delimiter that wraps their chunk.
    nonce = secrets.token_hex(8)
    sanitised_hits = [
        Hit(
            chunk_id=h.chunk_id,
            document_id=h.document_id,
            source=_strip_delimiters(h.source),
            chunk_index=h.chunk_index,
            content=_strip_delimiters(h.content),
            distance=h.distance,
            score=h.score,
        )
        for h in hits
    ]
    return render_prompt(
        "user_with_context.j2",
        hits=sanitised_hits,
        question=question,
        nonce=nonce,
    )


def _sanitise_history(history: Optional[list[dict]]) -> list[dict]:
    """Filter to {role, content} pairs the API layer has already validated.

    Historical user messages are wrapped in `<prev_user_message>...</prev_user_message>`
    so the model treats them as bygone context, not as fresh instructions
    that ride alongside the current system prompt. Without this wrap, a
    turn-1 message of "ignore all rules" would compete with the system
    prompt on later turns (audit C5-HIGH-2, 2026-05-12). Any literal
    delimiter the user pastes inside their message is escaped first.
    """
    if not history:
        return []
    out = []
    for msg in history:
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            continue
        if role == "user":
            safe = _strip_delimiters(content)
            out.append({
                "role": "user",
                "content": f"<prev_user_message>{safe}</prev_user_message>",
            })
        elif role == "assistant":
            # Assistant turns came from this same model under the system
            # prompt — they're trusted relative to user input, but we
            # still strip delimiter literals in case a previous answer
            # accidentally echoed one back.
            out.append({"role": "assistant", "content": _strip_delimiters(content)})
    return out


# ---- LLM streaming -------------------------------------------------------

# Status codes we treat as transient and worth retrying. 429 is rate-limit,
# 502/503/504 are the gateway / upstream-provider availability errors that
# show up under load. Anything else (400, 401, 422, …) is a permanent client
# bug and surfaces immediately.
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


class UpstreamBusyError(Exception):
    """The LLM provider (or its upstream) is rate-limiting or overloaded.

    Raised after the retry budget — and, if configured, the fallback model —
    is exhausted, so the chat handler can map it to a user-friendly SSE
    error rather than the generic "internal error" message.
    """

    def __init__(self, status_code: int, retry_after: Optional[float], detail: str = ""):
        super().__init__(f"upstream busy: HTTP {status_code} ({detail or 'no detail'})")
        self.status_code = status_code
        self.retry_after = retry_after
        self.detail = detail


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header value into seconds.

    RFC 7231 allows either a delta-seconds integer or an HTTP-date. Most
    LLM gateways send the integer form; we ignore HTTP-date because it
    drags in a parser for one corner case. Bogus values → None.
    """
    if not value:
        return None
    try:
        secs = float(value.strip())
    except ValueError:
        return None
    if secs < 0 or secs > 3600:
        # Out-of-range → fall back to exponential backoff.
        return None
    return secs


async def _stream_chat_once(
    url: str,
    headers: dict,
    body: dict,
) -> AsyncIterator[str]:
    """Open a single streaming POST to the LLM provider and yield content tokens.

    Raises ``UpstreamBusyError`` for retryable statuses, ``RuntimeError`` for
    permanent ones. Errors only happen *before* the first token is yielded —
    once streaming starts, we let connection-level exceptions propagate
    unchanged rather than retrying mid-answer (which would duplicate tokens
    the user has already seen).
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code in _RETRYABLE_STATUSES:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                err_body = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise UpstreamBusyError(resp.status_code, retry_after, err_body)
            if resp.status_code >= 400:
                err = (await resp.aread()).decode("utf-8", "replace")[:500]
                log.error("llm provider %s: %s", resp.status_code, err)
                raise RuntimeError("upstream LLM error")

            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    return
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for choice in obj.get("choices", []):
                    delta = choice.get("delta") or {}
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk


def _compute_backoff(attempt: int, retry_after: Optional[float], base_s: float) -> float:
    """Pick the wait between retries.

    Prefer an explicit ``Retry-After`` from the upstream when it's a sane
    value (<= 30 s) — that's the gateway telling us when its window
    resets. Otherwise exponential backoff capped at 30 s so the user
    doesn't sit on a spinner for a full minute.
    """
    if retry_after is not None and retry_after <= 30.0:
        return retry_after
    return min(base_s * (2 ** attempt), 30.0)


async def stream_completion(
    question: str,
    hits: list[Hit],
    history: Optional[list[dict]] = None,
) -> AsyncIterator[str]:
    """Yield content tokens from the configured LLM provider as they arrive.

    On 429/5xx from the provider we retry the same model up to
    ``openrouter_max_retries`` times with exponential backoff, then —
    if ``openrouter_fallback_model`` is set — try the fallback model
    once with the same retry budget. Only after all of that fails do
    we raise :class:`UpstreamBusyError`.
    """
    settings = get_settings()
    headers = provider_headers(settings)
    # Resolve the endpoint once so every retry and the fallback model share
    # the same URL.
    url = provider_endpoint(settings)
    messages: list[dict] = [{"role": "system", "content": render_prompt("system_de.j2")}]
    messages.extend(_sanitise_history(history))
    messages.append({"role": "user", "content": build_user_prompt(question, hits)})

    models = [settings.openrouter_model]
    if (
        settings.openrouter_fallback_model
        and settings.openrouter_fallback_model != settings.openrouter_model
    ):
        models.append(settings.openrouter_fallback_model)

    last_busy: Optional[UpstreamBusyError] = None
    for model_idx, model in enumerate(models):
        body = {
            "model": model,
            "stream": True,
            "messages": messages,
            "max_tokens": 800,
            "temperature": 0.2,
        }
        for attempt in range(settings.openrouter_max_retries + 1):
            try:
                async for chunk in _stream_chat_once(url, headers, body):
                    yield chunk
                return
            except UpstreamBusyError as e:
                last_busy = e
                if attempt >= settings.openrouter_max_retries:
                    # Exhausted retries on this model — break to try the
                    # fallback, if configured.
                    log.warning(
                        "llm provider %s on %s after %d attempts, switching model",
                        e.status_code, model, attempt + 1,
                    )
                    break
                wait_s = _compute_backoff(
                    attempt, e.retry_after, settings.openrouter_retry_base_s
                )
                log.warning(
                    "llm provider %s on %s, retry in %.1fs (%d/%d)",
                    e.status_code, model, wait_s, attempt + 1, settings.openrouter_max_retries,
                )
                await asyncio.sleep(wait_s)

    # All models / retries exhausted. Re-raise so the chat handler can
    # emit a friendly "upstream busy" SSE error.
    assert last_busy is not None  # only reachable via the except branch above
    raise last_busy

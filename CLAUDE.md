# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Internal-support RAG chatbot. Employees log in, ask questions, get answers grounded in company docs (PDF/MD/TXT) with source citations. Stack: FastAPI + Postgres/pgvector + local `intfloat/multilingual-e5-base` embeddings + `BAAI/bge-reranker-v2-m3` cross-encoder reranker + an LLM (OpenRouter by default, or a local OpenAI-compatible vLLM/Ollama via the provider seam) + vanilla JS frontend behind nginx, with Caddy terminating TLS in production. The UI is German; LLM prompts live as Jinja2 templates in `backend/prompts/` (see `system_de.j2` for the answering system prompt).

The product has grown four operator-facing capabilities beyond the base chatbot, each gated and off by default: **white-label branding** (operator logo + legal identity, migration 014), **multi-source ingest** (URL connector with an SSRF guard + optional ClamAV upload scanning), **a local-LLM option** (the `llm_provider` seam + `llm` compose profile), and a **CRM / external-DB sync** (`crm` compose profile) that anonymizes records before embedding. See the dedicated sections below — these are where most of the post-May-2026 code and the sharpest footguns live.

## Common commands

All work happens through docker compose; there is no local Python venv flow.

```bash
# Bring stack up (or rebuild after code changes)
docker compose up -d --build

# Recreate backend so it re-reads .env (restart does NOT reload env)
docker compose up -d --no-deps --force-recreate backend

# Ingest docs from ./docs (mounted into backend at /app/docs)
docker compose exec backend python ingest.py /app/docs

# Optional profiles (off by default; each needs its own .env flag too):
#   security -> ClamAV upload scanner (CLAMAV_ENABLED=true)
#   llm      -> local OpenAI-compatible model server (LLM_PROVIDER=vllm|ollama)
#   crm      -> one-off CRM/external-DB anonymizing sync (CRM_ENABLED=true)
docker compose --profile security up -d --build
docker compose --profile llm up -d --build
docker compose --profile crm run --rm crm-sync --spec /app/crm/spec.json

# Create or reset a user (prompts for password; min 12 chars)
docker compose exec backend python -m auth add-user user@company.com
docker compose exec backend python -m auth add-user user@company.com --admin

# End-to-end smoke test (spins stack, ingests, logs in, runs one chat)
./smoke_test.sh

# Tail backend logs
docker compose logs -f backend

# Force-reingest a single file: delete its row, then re-run ingest
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "DELETE FROM documents WHERE source = 'handbook.pdf';"

# Pytest unit suite (token/HMAC, password, prompts, history formatting).
# Runs in <1s, no DB or model load. Integration tests (live stack +
# OpenRouter) are excluded by default; opt in with `pytest -m integration`.
docker compose exec backend pip install --user -r requirements-test.txt
docker compose exec backend pytest
```

The smoke test is the canonical "does the wired stack still work" check; the pytest suite covers the helpers (no DB, no model). CI (`.github/workflows/test.yml`) runs the unit suite on every push and PR. There's still no linter configured.

## Architecture

Three always-on containers on a private docker network; only `frontend` (nginx) publishes a port. `backend` and `postgres` are reachable only inside the compose network — never expose them. Three more services are profile-gated and off by default: `clamav` (profile `security`), `llm` (profile `llm`), and `crm-sync` (profile `crm`, a one-shot run, not a daemon).

- `frontend/nginx.conf` serves static files and proxies `/api/*` to `backend:8000`. Security headers (CSP, X-Frame-Options, etc.) are repeated in **every** `location` block — nginx drops server-level `add_header` as soon as any location adds one. Don't "DRY" them up.
- `backend/main.py` is the FastAPI entrypoint: lifespan (migrations → pool), CORS, slowapi rate limiter, `SecurityHeadersMiddleware`, and routes. The chat endpoint streams Server-Sent Events; nginx has `proxy_buffering off` for the API location so tokens arrive live.
- `backend/auth.py` owns sessions: cookie value is `<random_id>.<hmac_sha256(random_id)>`; the DB stores only `sha256(random_id)`. A leaked DB doesn't yield live cookies. Login always runs bcrypt (real or dummy hash) to keep timing constant across known/unknown emails. Per-account lockout (10 fails / 15 min) sits on top of slowapi's per-IP throttle.
- `backend/rag.py` does retrieval (hybrid: `pgvector` `<=>` cosine + Postgres FTS BM25 with German stemming, deduplicated then cross-encoder-reranked with `bge-reranker-v2-m3` and a 0.3 sigmoid-score threshold) + streamed completion from OpenRouter. `retrieve()` requires `is_admin` and `allowed_tags` as **kwarg-only** args — the ACL filter (`backend/acls.py:visibility_clause`) is appended to both the dense and BM25 WHERE clauses so a forgotten parameter is a TypeError, not a silent data leak. All prompts are Jinja2 templates in `backend/prompts/` (rendered via `prompts.render(name, **vars)`); the system template wraps retrieved chunks in `<context>...</context>` and tells the model to treat that block as untrusted data. The user's chat message goes only in the `user` role message, never the system prompt.
- `backend/acls.py` owns tag normalisation (lowercase, strip, ascii slug, max 32 chars), the SQL `visibility_clause` builder, AND `assert_known_departments()` which validates incoming tags against the controlled `departments` vocabulary table (migration 012). The visibility clause is used in three places — `rag.py retrieve()`, `main.py list_documents_public`, and `main.py get_document` — and the validator is used in four — `admin.upload_document`, `admin.update_document_tags`, `admin.create_user`, `admin.update_user_allowed_tags`. Adding a fifth tag-write surface without routing through `assert_known_departments()` is the canonical way to seed unknown tags that the picker can never untag — don't.
- `backend/telemetry.py` owns the OpenTelemetry tracer setup. **Off by default** — spans are created (so call sites can use `tracer.start_as_current_span(...)` unconditionally) but no exporter runs unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set (OTLP/HTTP). `OTEL_CONSOLE_EXPORTER=true` adds a stdout exporter for local debugging. Auto-instrumentation is wired for FastAPI (request spans, `/health` excluded) and httpx (OpenRouter calls). Manual spans inside the chat pipeline: `chat.stream`, `chat.llm.stream`, `rag.retrieve`, `rag.embed_query`, `rag.retrieve.{dense,bm25}`, `rag.rerank`, `rag.rewrite` (only when history is non-empty). Span attributes are metadata only — counts, scores, IDs, model names — **never** query text or document content (same A09 rule as logs).
- `backend/cleanup.py` is a tiny background task started by `lifespan` that reaps expired `sessions` and `login_challenges` rows every 6 h (audit L-6, 2026-05-10). Failures log loudly but never crash the loop. The first pass runs immediately on startup so a long-stopped deployment catches up.
- `backend/.pip-audit-ignore` lists CVEs explicitly accepted in the `cve-scan` CI job. Each entry has a written justification (threat model, mitigation in code, target re-eval date). Adding an entry here is the only way to silence pip-audit — never `--ignore-vuln` ad-hoc in the workflow.
- `backend/ingest.py` is the offline document pipeline (CLI). `backend/admin.py` reuses the same parse/chunk/embed/insert helpers for the in-app upload endpoint. `_insert_document_with_chunks` is one transaction per document — a crash mid-embed must not leave an orphan `documents` row (the file-hash unique constraint would otherwise block re-ingest forever).
- `backend/db.py` runs `apply_migrations()` **before** `init_pool()` because the pool's configure callback registers the pgvector type adapter, which needs the `vector` extension to already exist. Migrations are plain SQL files in `backend/migrations/` applied in lexical order, tracked in `schema_migrations`; each must be self-idempotent.
- `backend/llm_provider.py` is the **provider seam**: the chat pipeline speaks one OpenAI-compatible Chat Completions contract, and this module is the *only* place that knows the per-provider endpoint URL and headers (`openrouter` vs local `vllm`/`ollama`). `rag.py` calls `provider_endpoint(settings)` / `provider_headers(settings)` and is otherwise provider-agnostic — same body, same SSE parsing, same retry/fallback. OpenRouter attribution headers (`HTTP-Referer`/`X-Title`) are sent *only* for OpenRouter; local servers reject unknown headers. Switching providers is `LLM_PROVIDER` + `LLM_BASE_URL` in `.env` plus the `llm` compose profile — no code change.
- `backend/secret_box.py` wraps at-rest secrets (TOTP seeds today) in Fernet with a key from `TOTP_KMS_KEY` (kept in `.env`, separate from the DB — audit C6-HIGH-6). `unwrap()` is back-compat: it tries Fernet decrypt and, on `InvalidToken`, returns the input unchanged as legacy plaintext, so existing plaintext rows keep working until an operator runs a rewrap pass. A missing key logs a loud one-shot WARNING and falls back to plaintext rather than crashing.
- **Connectors (`backend/connectors/`) are two different shapes for two pipelines.** A *document* connector (`local.py`, `url.py`) yields raw bytes/`SourceItem`s into the normal parse→chunk→embed path. `url.py` is the multi-source-ingest feature and is security-critical: on every request *and every redirect hop* it enforces an http/https scheme allow-list, an SSRF guard (resolves the host, rejects private/loopback/link-local/`169.254.169.254` metadata ranges), and a byte cap enforced *while streaming* (never trusting `Content-Length`). It strips scripts/style/tags so the hashed text is stable across re-fetches. Tags are NEVER derived from URL structure — the caller must pass an explicit ACL decision. The *record* connector (`sql_connector.py`, base types in `base.py`) is a different envelope: it yields structured `ExternalRecord`s that must be anonymized **before** any text is hashed/embedded/stored.
- **CRM sync (`backend/crm_sync.py` + `backend/pii.py`) runs OFFLINE in the one-shot `crm-sync` container — never in the always-on backend and never on the chat path.** For each row of the operator-authored read-only SELECT it: resolves ACL tags fail-closed (`record_to_tags`→`normalise_tags`→`assert_known_departments`; unknown/ambiguous → `['crm_unmapped']`, never public/`[]`), anonymizes via `pii.anonymize` (declared per-field `PiiAction` drop/pseudonymize/keep first, then a Presidio NER backstop on free text that fails *closed* — drops free text entirely if Presidio is missing), hashes the *scrubbed* text, chunks+embeds with the same `Embedder` as `ingest.py`, and version-inserts with `source='crm:sql:<source_id>'` (`source_id` must be a stable non-PII key — never an email/name). Pseudonymization is `hmac_sha256(value, PII_PSEUDONYM_KEY)[:12]` — deterministic so re-syncs don't churn versions; rotating the key changes every pseudonym. One `admin_audit` row per run, **counts only**. Presidio's ~1.5 GB of NLP models live only in `Dockerfile.sync`/`requirements-sync.txt`, which is why this is a separate container.
- **ClamAV upload scanning** (`backend/admin.py:_clamd_scan`) is opt-in via `CLAMAV_ENABLED` + the `security` profile. It scans uploaded bytes over clamd's INSTREAM TCP protocol and **fails closed** — a clamd outage refuses the upload rather than waving it through.

## Things that will trip you up

- **No `from __future__ import annotations` in any module that hosts a `@limiter.limit`-decorated route.** Currently `backend/main.py` and `backend/totp.py`. slowapi's decorator wraps routes in a function whose `__globals__` don't see our Pydantic body models, so FastAPI can't resolve string ForwardRefs and reclassifies the body as a query param (every login → 422; `/api/me/2fa/verify` → 500 with `PydanticUserError`). Keep concrete annotations and explicit `Body(...)` defaults on rate-limited routes. The shared `Limiter` instance lives in `backend/rate_limit.py` so any router (currently `main`, `totp`) can import it without an import cycle.
- **`docker compose restart` does not reload `.env`.** Use `up -d --force-recreate <service>`.
- **Embedding dim is hardcoded** (`embedding_dim=768` in `config.py`, matching `multilingual-e5-base`). Swapping the embedding model requires a migration of the `chunks.embedding` column and a re-ingest — don't just change the env var. Migration `003_multilingual_embeddings.sql` is the precedent: drop HNSW index, TRUNCATE chunks/documents, ALTER COLUMN, recreate index.
- **E5 prefixes are mandatory.** `multilingual-e5-base` requires `query: ` on retrieval queries (`backend/rag.py:embed_query`) and `passage: ` on document chunks (`backend/ingest.py:Embedder.encode`). Without the prefix the model still returns vectors, but retrieval quality silently degrades — symptoms look like "RAG just isn't finding things" rather than a crash.
- **Two models in-process, ~850 MB resident.** Embedder (`multilingual-e5-base`, ~280 MB) + reranker (`bge-reranker-v2-m3`, ~570 MB) both live in the backend. The 2 GB VPS recommendation in the README is now tight — bump to 4 GB minimum. Models are pre-warmed in the FastAPI lifespan (`rag.warmup()`) so the first user no longer pays the cold-start cost; if warmup fails for any reason the lazy loaders take over silently.
- **Torch is pinned to single-thread mode for concurrency.** `rag._configure_torch_for_concurrency()` runs `torch.set_num_threads(1)` + `set_num_interop_threads(1)` **before** the first model load. This intentionally trades single-request latency (~2.85 s → ~3.5 s rerank on 4 cores) for parallel throughput: with the default "use all cores per inference" two concurrent reranks each drop to half speed, but with one core each, four reranks run truly in parallel on a 4-core host. For 10–20 concurrent users this is the difference between a 14-second tail and a 60-second tail. Don't undo it without measuring under burst load — and never call `torch.set_num_interop_threads` from anywhere else (torch forbids the second call).
- **Conversation history is now persistent and server-owned.** Migration 005 added `conversations` and `messages` tables. `backend/conversations.py` exposes CRUD at `/api/conversations/*`; the chat handler in `main.py` reads history from the DB by `conversation_id` and writes back the user turn before retrieval and the assistant turn at stream end. The client no longer sends `history` — it sends `conversation_id` (or null to start fresh; the new id comes back via the SSE `meta` event). Privacy implication: message *content* is now stored, not just metadata. The Datenschutz page discloses this; see SECURITY.md A09 for the prior posture.
- **Avoid OpenRouter "reasoning" models** (`:reasoning`, `-r1`, `thinking` slugs, `o1-*`, `deepseek-r1`, `qwen3-thinking`) unless you raise `max_tokens` substantially; they spend their budget on hidden CoT and emit zero answer tokens at the default 800 cap. Also avoid `:free` tiers (rate-limited, frequent token-stream dropouts) and specialized fine-tunes like `gpt-oss-safeguard-20b` (safety classifier, not a chat model). Recommended non-thinking models for this stack live in README §4 — default is `anthropic/claude-haiku-4-5`; `mistralai/mistral-medium-3.1` is the EU-vendor alternative for DSGVO-strict deployments.
- **OpenRouter 429/5xx is retried inside `stream_completion`.** All users share one `OPENROUTER_API_KEY`, so during traffic spikes (workshops, demos) the upstream rate-limits before our per-user / per-IP caps even fire. `backend/rag.py` retries the same model `openrouter_max_retries` times with exponential backoff (respecting `Retry-After` when sane), then — if `openrouter_fallback_model` is set in `.env` — tries that model with the same budget before raising `UpstreamBusyError`. The chat handler maps that to a distinct SSE error (`detail: "upstream_busy"`); the frontend renders a "Modell ist gerade ausgelastet" hint instead of the generic error string. The retry only happens *before* the first token is yielded — connection drops mid-answer propagate as-is, because retrying then would duplicate tokens the user has already seen.
- **The LLM is the dominant latency cost** (~75% of TTFT, measured 2026-05). Local CPU embeddings (~45 ms) + DB retrieval (~5 ms) + reranker (~2.85 s) are all eclipsed by LLM TTFT (~3-12 s depending on model). Don't optimize embeddings/rerank before fixing the model choice. Measurement breakdown is in README §4.
- **Pinned deps everywhere** (`backend/requirements.txt`, base image tags). Bumping is a deliberate review, not a casual edit.
- **Containers run as non-root** (`uid 10001` for backend, nginx user for frontend). The `hfcache` volume must be pre-created with `app:app` ownership in the Dockerfile so Docker's "copy image content into empty volume on first mount" preserves ownership — without this, the model download fails on first boot.
- **No public signup, no password reset over the API.** User management is admin-only via `/api/admin/users` or the `auth add-user` CLI. Don't add a signup route.
- **Tag inheritance on re-upload.** `_insert_document_with_chunks` in `backend/ingest.py` inherits tags from the previous current version when the caller passes `tags=None`. The admin upload endpoint passes `None` when the multipart `tags` field is empty, so re-uploading a doc to fix a typo doesn't accidentally drop the ACL. To make a previously-tagged doc public, send `tags="[]"` explicitly.
- **`origin` (migration 015) and `source_type` (016) are two separate metadata dimensions — and BOTH are metadata only.** `origin` is fetch-layer provenance (`local`/`url`); `source_type` is the ingest-channel class (`file`/`crm`). A CRM document has `source_type='crm'` but leaves `origin` at its `local` default (it isn't an HTTP fetch). Neither participates in dedup (still keyed on `content_hash`) or the ACL (still keyed on `tags`) — don't reach for them to gate visibility.
- **Never grant the `crm_unmapped` department to a user.** It's the fail-closed landing tag (migration 016) for CRM records whose group the operator mapping couldn't resolve. It's a real entry in the controlled vocabulary so `assert_known_departments()` accepts it, but granting it to anyone exposes every unmapped record. The CRM right-to-erasure (Art. 17) tombstone pass is a documented NOT-done item — a deleted upstream row leaves its last anonymized version in place until manually removed.
- **A local LLM doesn't escape the model-choice footguns.** The reasoning-model / `max_tokens` caveat above applies to vLLM/Ollama too — a local thinking model still burns its budget on hidden CoT at the 800-token default. `LLM_PROVIDER=vllm|ollama` *requires* `LLM_BASE_URL`; `config.py` validates this at startup. `llm_provider.py` may keep `from __future__ import annotations` (it hosts no rate-limited route) — the ForwardRef ban is only for `main.py`/`totp.py`.

## Security posture

This app is designed to face the public internet. `SECURITY.md` is the OWASP Top-10 self-review and the authoritative spec for what's defended; read it before changing auth, sessions, headers, CORS, the system prompt, or the rate limits. In particular: keep all SQL parameterised (psycopg `%s`), keep generic error messages on `/api/login`, never log query content or session ids, and don't loosen the CSP without updating both `backend/main.py` and `frontend/nginx.conf` in lockstep.

Production hardening that lives in code (added in migration `004_security_features.sql` and the surrounding handlers):

- **Per-user daily chat quota** in `chat_usage(user_id, day, request_count)`. Enforced in `backend/main.py` chat handler — UPSERTs, then 429 if `request_count > settings.chat_daily_limit_per_user`. Counts every authed call (including those that only run the rewrite step), because the rewrite step costs OpenRouter tokens too.
- **Admin audit log** in `admin_audit(actor_id, action, target_type, target_id, details, ip, occurred_at)`. Every mutating endpoint in `backend/admin.py` calls `_audit(...)` after a successful action; `actor_id` has `ON DELETE RESTRICT` so deleting an admin who's done admin work errors out — by design.
- **Audit writes are best-effort.** The mutation has already committed by the time `_audit` runs; an audit failure logs an exception but does not roll back. The trade-off is consistency: occasional missed audit rows vs. blocking real admin work on a transient DB hiccup.
- **Static legal pages.** `/impressum` and `/datenschutz` are served by nginx via `try_files $uri $uri.html ...` from `frontend/{impressum,datenschutz}.html`. They're DSGVO/TMG placeholder text — the operator must fill in the bracketed fields and have a lawyer sign off before going live.
- **Backups.** `./backup.sh` shells into the postgres container and writes `pg_dump --format=custom` to `./backups/`. Optional `age` encryption via `BACKUP_AGE_RECIPIENT`. Retention defaults to 14 days. The `backups/` directory is gitignored.

## Operational docs

Long-form ops notes live under `docs-ops/` (separate from the README to keep the README task-focused on bringup):

- **`docs-ops/log-shipping.md`** — how to ship container logs to Grafana Loki (Free-Tier flow, plugin install, compose driver config).
- **`docs-ops/scaling-500-plus.md`** — phased strategy for going from today's 10–20 concurrent-active capacity to 500+ users. Names the actual bottlenecks (reranker CPU, OpenRouter rate limit, single Postgres), defines entry triggers per phase, and takes an explicit position on *not* introducing Kubernetes prematurely. Consult this before suggesting horizontal-scaling or architecture changes — the doc has already weighed the trade-offs we'd otherwise re-derive in chat.
- **`docs-ops/vvt.md`** — Verzeichnis der Verarbeitungstätigkeiten (DSGVO Art. 30) template. Operator-filled before go-live; kept in lockstep with `datenschutz.html` retention values.
- **`docs-ops/dns.md`** — CAA / SPF / DMARC / DNSSEC / Registrar-Lock / HSTS-Preload checklist for the production domain. Outside-repo work but required before go-live.
- **`docs-ops/backup-offsite.md`** — 3-2-1 backup setup (rclone → S3/B2 with Object-Lock) and the quarterly restore-drill protocol.
- **`docs-ops/postgres-hardening.md`** — staged migration to privilege-separated migration user + TLS between backend and Postgres. Not enforced in compose defaults; consult before scaling beyond single-VPS.
- **`docs-ops/incident-response.md`** — IR plan, DSGVO Art. 33/34 templates, eskalations-baum, jährlicher Tabletop-Test. Read before Go-Live; rotate `SESSION_SECRET` / `OPENROUTER_API_KEY` / `TOTP_KMS_KEY` cheatsheet at the bottom is the operational reference if anything ever leaks.
- **`docs-ops/doc-generators.md`** — explains the AI-generated `build-*-docs.js` scripts at the repo root and the path to converting them to a proper `package.json` flow if they ever stop being one-shot tools.

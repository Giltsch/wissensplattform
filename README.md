# Internal Support RAG Chatbot

A small, opinionated, production-leaning RAG chatbot for internal company
documents. Employees log in, ask questions, and get answers grounded in the
docs you ingest, with source filenames cited in each response.

![tests](https://github.com/Giltsch/wissensplattform/actions/workflows/test.yml/badge.svg)

**Highlights**

- **Hybrid retrieval**: pgvector cosine + Postgres full-text (German stemming), merged and
  re-ranked by a local cross-encoder (`bge-reranker-v2-m3`); embeddings run locally, so
  only the LLM prompts leave the server
- **Document-level access control**: department tags enforced inside the SQL of both
  retrieval paths, so a forgotten filter is a `TypeError`, not a data leak
- **Security first**: HMAC session cookies, constant-time login, account lockout, TOTP 2FA
  with encrypted seeds, SSRF-guarded URL ingest, optional ClamAV scanning, strict CSP,
  prompt-injection hardening. Threat model and OWASP Top 10 review in [SECURITY.md](SECURITY.md)
- **Pluggable LLM**: OpenRouter or a local vLLM/Ollama server, with retry/fallback on 429s
- **DSGVO-aware**: CRM sync that pseudonymizes records *before* embedding, retention
  cleanup, audit log, VVT and incident-response templates in [`docs-ops/`](docs-ops)
- **Ops**: one `docker compose up`, pinned deps, CI with unit tests, pip-audit, Trivy and SBOM,
  OpenTelemetry tracing, backups with optional `age` encryption

- **Backend**: Python 3.11 + FastAPI
- **Vector DB**: Postgres 16 + [pgvector](https://github.com/pgvector/pgvector)
- **Embeddings**: `intfloat/multilingual-e5-base` via `sentence-transformers` (runs locally — no embedding API cost). German-capable (the corpus is German).
- **Reranking**: `BAAI/bge-reranker-v2-m3` cross-encoder over the top-K dense+BM25 candidates, with a relevance threshold below which the LLM is asked the question with an empty context block (routes to a refusal).
- **Prompts**: Jinja2 templates in `backend/prompts/` (system, rewrite, user-with-context, rewrite-user) — keeps prompt edits out of Python.
- **LLM**: any model on [OpenRouter](https://openrouter.ai), configurable by env
- **Frontend**: vanilla HTML / JS / CSS served by nginx
- **TLS**: Caddy in front (auto Let's Encrypt) — example config in `Caddyfile.example`

> **Threat model:** this app is intended for deployment on the public internet.
> Security is non-negotiable; see [Security](#security) for what's enforced.

---

## Prerequisites

- Docker 24+ and Compose v2
- An OpenRouter API key
- A domain (only required for HTTPS — local dev runs on `http://localhost:8080`)

---

## 1. Local run

```bash
cp .env.example .env       # then edit values — see notes below
docker compose up -d --build
```

When healthy:

```bash
docker compose ps
# All three services should show STATUS: Up (healthy)
```

Open <http://localhost:8080>. You'll be prompted to log in. Create a user
first (next section).

### `.env` notes

- `POSTGRES_PASSWORD` and `SESSION_SECRET` must be unique random strings.
  - `openssl rand -hex 32` is a good source.
- Leave `POSTGRES_HOST=postgres` — it's the docker-compose service name.
- `COOKIE_SECURE=false` for local dev (HTTP). Set to `true` once you're behind HTTPS.
- `ALLOWED_ORIGIN` should match the URL you use in the browser (e.g. `https://support.example.com`).

---

## 2. Ingesting documents

Drop `.pdf`, `.md`, or `.txt` files into the `docs/` directory at the repo
root, then run:

```bash
docker compose exec backend python ingest.py /app/docs
```

What this does:

1. Walks the directory and parses each supported file.
2. Splits each document into ~500-token chunks with 50-token overlap.
3. Computes a sentence-transformers embedding per chunk.
4. Inserts everything into Postgres, with `ON CONFLICT DO NOTHING` based on
   content hashes — re-running on unchanged input is a no-op.

To re-ingest a file that *has* changed, delete its row first:

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "DELETE FROM documents WHERE source = 'handbook.pdf';"
```

The chunks cascade-delete; the next ingest run will re-create them.

---

## 3. Adding (or resetting) a user

```bash
docker compose exec backend python -m auth add-user user@company.com
# Prompts for password (twice). Min 12 chars.
# If the user already exists, this resets the password and clears any lockout.
```

There's no signup flow. Users are created by an operator. This is intentional
for an internal tool.

### Document access control (departments)

By default every authenticated user sees every document — fine for
general internal docs, not fine for HR / legal / finance content.
Restrict access by creating **departments** in the admin tab and tagging
documents + users with them.

In the admin UI:
1. **Dokumente → Abteilungen** — create departments (label + slug). The
   slug is the canonical id stored on documents/users; the label is
   shown in pickers.
2. **Upload form / „Tags bearbeiten"** — multi-select chip picker over
   the created departments. No free text — the server rejects unknown
   slugs.
3. **Benutzer → Tag-Berechtigungen** — pick which departments a user
   may see.

Visibility rule, enforced by `backend/acls.py:visibility_clause` in
`retrieve()`, `list_documents_public()`, and `get_document()`:

- **Admin** → sees everything (bypass).
- Document with **no departments assigned** → public, every authed user sees it.
- Document with departments → user must have at least one matching
  department in `allowed_tags`.

Re-uploading a doc without specifying departments **inherits** the
previous version's selection — so fixing a typo doesn't accidentally
make a restricted doc public. Send an explicit empty list to make it
public.

Deleting a department from the vocabulary strips it from every
document/user array in the same transaction — no orphaned tags that
the picker can never offer.

---

## 3a. White-label branding

The app ships with **neutral defaults** (app name `Wissensplattform`, no
sub-brand, accent `#2563EB`, a generic inline-SVG logo). Nothing
company-specific is baked into the code or images.

**The admin Branding tab is the single source of truth.** It writes the
singleton `branding` row (migration 014) and is read by the public
`GET /api/branding`, which the SPA and the legal pages fetch before login.
There you can set:

- **App-Name / Sub-Brand** — the wordmark shown in headers, the login
  page, and `<title>`. Sub-brand is optional (leave it empty for a
  single-word brand).
- **Accent colour** — applied live to the `--accent` CSS variable
  (validated as `#RGB`/`#RRGGBB`, same regex front and back).
- **Logo** — an SVG or PNG up to 192 KB, stored as an inline
  `data:` URI (CSP-safe: `img-src 'self' data:` already permits it; SVG is
  rendered via `<img src>` so it cannot execute scripts). Used for both the
  on-screen logo and the favicon. "Auf Standard zurücksetzen" clears it
  back to the neutral mark.
- **Legal identity** — Firma, Anschrift, Vertreter, Kontakt-/Datenschutz-
  E-Mail, Registereintrag, USt-ID, Aufsichtsbehörde. These fill the
  `data-brand` placeholders on `/impressum` and `/datenschutz`; unset
  fields keep their bracketed placeholder so an operator can see what's
  still missing. The German legal boilerplate (TMG/DSGVO text, the
  OpenRouter/model processor disclosure) stays in the HTML — only the
  operator-specific fields are data-driven.

`OPENROUTER_TITLE` / `OPENROUTER_REFERER` are **not** branding — they are
LLM-attribution metadata sent to OpenRouter only.

The build-time docs generators (`build-user-docs.js`, `build-dev-docs.js`)
also default to the neutral name and accept `BRAND_APP_NAME`,
`BRAND_SUB_BRAND`, and `BRAND_ACCENT` (hex without `#`) env overrides.

---

## 4. Choosing & swapping the LLM

OpenRouter exposes most major models via one OpenAI-compatible endpoint.
The chat model is the **dominant latency driver** in this stack — local
embedding (~50 ms) and reranking (~3 s) are both cheap compared to LLM
TTFT, so picking the right model matters more than tuning anything else.

### Recommended models (no thinking / no reasoning variants)

| Slug | Strength | TTFT | Price (in / out per 1M) | When to pick |
|---|---|---:|---|---|
| **`anthropic/claude-haiku-4-5`** | Best price × quality × speed; near-native German | 1.5–3 s | ~$1 / ~$5 | **Default recommendation** for an internal support bot |
| `anthropic/claude-sonnet-4-6` | Best non-thinking reasoning; strong source fidelity | 2–4 s | ~$3 / ~$15 | When answer quality matters more than cost |
| `google/gemini-2.5-flash` | Very fast, very cheap, 1M-token context | 1–2 s | ~$0.30 / ~$2.50 | Tightest budget or highest QPS |
| `mistralai/mistral-medium-3.1` | EU vendor (France); cleaner DSGVO posture; native German | 2–4 s | ~$0.40 / ~$2 | When data-sovereignty review demands an EU provider |
| `openai/gpt-oss-120b` | Open weights — possible self-host migration path later | 3–6 s | ~$0.10 / ~$0.50 | When you may move off OpenRouter onto own infra |

(Prices are rough Q2-2026 indicators; OpenRouter's dashboard is the
source of truth and varies with provider routing.)

### What to avoid

- **`:free` tiers** (`openai/gpt-oss-20b:free`, `…:free` in general) — heavily
  rate-limited, routed to slow/unstable providers, and frequently emit zero
  tokens before timing out. Fine for one-shot pipeline verification, not for
  real use.
- **Reasoning / thinking models** (`:reasoning`, `:thinking`, `-r1`,
  `o1-*`, `deepseek-r1`, `qwen3-thinking`, …) — they spend their token
  budget on hidden chain-of-thought and emit zero answer tokens at the
  default 800 `max_tokens` cap. Either raise the cap substantially or
  pick a non-thinking sibling.
- **Specialized fine-tunes presented as chat** (e.g. `gpt-oss-safeguard-20b`
  is a content-moderation classifier, not a Q&A model) — they will produce
  strange, judgement-shaped output in a RAG flow.

### How to switch

Update `.env`:

```bash
OPENROUTER_MODEL=anthropic/claude-haiku-4-5
```

Then **recreate** the backend (not just restart) so the new env is picked up:

```bash
docker compose up -d --no-deps --force-recreate backend
```

> **Gotcha.** `docker compose restart` does **not** reload `.env`. It only
> sends SIGTERM to the running process. To re-read env vars, use
> `docker compose up -d` (which recreates the container) or
> `docker compose up -d --force-recreate <service>`.

No re-ingestion needed — embeddings are independent of the chat LLM.

### LLM provider: OpenRouter or a local server (vLLM / Ollama)

The chat pipeline speaks the OpenAI-compatible Chat Completions contract,
which OpenRouter, vLLM, and Ollama all implement. Switching between them
is just URL + headers (handled in `backend/llm_provider.py`); the retrieval,
prompt assembly, SSE parsing, and retry/fallback logic are identical.

| `LLM_PROVIDER` | Endpoint | Auth | Notes |
|---|---|---|---|
| `openrouter` (default) | hosted gateway | `OPENROUTER_API_KEY` required | Sends `HTTP-Referer`/`X-Title` attribution headers |
| `vllm` | `LLM_BASE_URL` (e.g. `http://llm:8000`) | key optional | GPU-first, high throughput |
| `ollama` | `LLM_BASE_URL` (e.g. `http://llm:11434`) | key optional | CPU-friendly, simplest to run |

For a local provider, `OPENROUTER_MODEL` holds the *served* model name
(the HuggingFace repo vLLM was launched with, or the Ollama tag) rather
than an OpenRouter slug. `OPENROUTER_API_KEY` can be left empty — when it
is, no `Authorization` header is sent.

To run a local model, use the opt-in `llm` compose profile (mirrors the
`security`/clamav profile — internal network only, no published port):

```bash
# 1. In docker-compose.yml, uncomment EXACTLY ONE of the vLLM / Ollama
#    image+command blocks under the `llm` service.
# 2. In .env:
#      LLM_PROVIDER=ollama
#      LLM_BASE_URL=http://llm:11434     # or http://llm:8000 for vLLM
#      OPENROUTER_MODEL=llama3.1          # the served model name
# 3. Start the profile and recreate the backend so it re-reads .env:
docker compose --profile llm up -d
docker compose up -d --no-deps --force-recreate backend
# Ollama only: pull the model once it's up
docker compose exec llm ollama pull llama3.1
```

> **DSGVO note.** With `LLM_PROVIDER=openrouter`, every chat sends the
> question + retrieved chunks to OpenRouter (and its routed sub-provider) —
> a third-country transfer (Art. 44 ff.) that `datenschutz.html` must
> disclose. A **local** provider (vLLM/Ollama on your own infra) **removes**
> that transfer: the chat never leaves your network. If you switch to a
> local provider, update `datenschutz.html` (and `docs-ops/vvt.md`) to
> drop the OpenRouter transfer disclosure.
>
> A local LLM needs substantially more RAM than the ~850 MB the
> embedder + reranker already use (and ideally a GPU for vLLM). Size the
> `llm` service's `mem_limit`/`cpus` to your host before enabling.

### Surviving traffic spikes (workshops, demos)

All users share one `OPENROUTER_API_KEY`, so a room full of people
asking questions at once hits OpenRouter's account-level rate limit
long before our per-user / per-IP caps fire. The backend handles this
without user-visible failure:

1. On `429`/`502`/`503`/`504` from OpenRouter the chat handler retries
   the same model `OPENROUTER_MAX_RETRIES` times (default 2) with
   exponential backoff (1 s → 2 s → 4 s, capped at 30 s). If the
   upstream sends a sane `Retry-After` header, we respect it instead.
2. If retries are exhausted **and** `OPENROUTER_FALLBACK_MODEL` is
   configured, the same request is tried once against the fallback —
   ideally a different vendor so a single provider's rate-limit
   pressure doesn't block both paths. Recommended pairing:

   ```bash
   OPENROUTER_MODEL=anthropic/claude-haiku-4-5
   OPENROUTER_FALLBACK_MODEL=mistralai/mistral-medium-3.1
   ```
3. Only if *all* attempts fail does the user see an error — and it's
   a German "Modell ist gerade ausgelastet, bitte gleich nochmal"
   hint, not the generic internal-error string.

Retries only happen *before* the first token is streamed. A
connection drop mid-answer propagates as a normal error because
restarting the stream would duplicate tokens the user already sees.

### Where the time goes (measured, warm cache, 5-query sample)

| Phase | Median | Share of TTFT |
|---|---:|---:|
| Embed query (E5-base, in-process) | ~45 ms | <1% |
| Dense retrieval (pgvector cosine) | ~2 ms | <1% |
| BM25 retrieval (Postgres FTS) | ~2 ms | <1% |
| **Cross-encoder rerank** (bge-v2-m3, 20 → 5 candidates) | **~2 850 ms** | ~20% |
| **LLM TTFT** (model-dependent) | **~3–12 s** | ~75–80% |

Conclusion: the LLM dominates. Local CPU embeddings are not the
bottleneck — don't move them to an embeddings API hoping for speed-up.
If you do need to shrink the reranker, lower `retrieve_k` in
`config.py` (default 20) or swap to an ONNX/int8 build of the same
checkpoint.

---

## 5. Smoke test

```bash
./smoke_test.sh
```

Spins up the stack, ingests a tiny doc, creates `smoke@example.com`, logs
in, and runs one chat query end-to-end. Exits non-zero on any failure.

---

## 6. Unit tests

Pytest suite for the helpers that don't need the running stack — token /
HMAC / password helpers, prompt template rendering, history formatting.
Runs in <1 s.

```bash
# Inside the running container (uses the pinned deps already in the image):
docker compose exec backend pip install --user -r requirements-test.txt
docker compose exec backend pytest

# Or on the host, with a venv:
pip install -r backend/requirements.txt -r backend/requirements-test.txt
cd backend && pytest
```

Tests marked `@pytest.mark.integration` need a live stack and an
`OPENROUTER_API_KEY`; they're excluded by default. Use `pytest -m
integration` to run only those, or `pytest -m ""` for everything.

CI runs the unit suite on every push and pull request — see
`.github/workflows/test.yml`.

---

## 7. VPS deployment (Hetzner / DigitalOcean)

A minimal walkthrough. Adjust to taste.

### 7.1 Provision

- Create a VPS. For 10–20 *concurrent active* users (e.g. workshop / demo
  load), aim for **4 vCPU / 4–8 GB RAM** (Hetzner CX32 or similar). Each
  vCPU handles roughly one parallel rerank pass at a time, so cores
  translate directly into the concurrent-chat ceiling. For a small
  internal user base (~50 registered, 1–2 concurrent at any moment) a
  2 vCPU / 4 GB box is still fine.
- Point an A record at it: `support.example.com → <server IP>`.

### 7.2 Initial host setup

```bash
ssh root@server
# Create a deploy user, install docker, basic firewall.
adduser deploy && usermod -aG sudo deploy
apt update && apt install -y docker.io docker-compose-plugin caddy ufw
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
```

### 7.3 Pull the code and start the stack

```bash
su - deploy
git clone <your-repo-url> rag && cd rag
cp .env.example .env
# Edit .env — set POSTGRES_PASSWORD, SESSION_SECRET, OPENROUTER_API_KEY,
# ALLOWED_ORIGIN=https://support.example.com, COOKIE_SECURE=true
docker compose up -d --build
```

### 7.4 TLS via Caddy

```bash
sudo cp Caddyfile.example /etc/caddy/Caddyfile
# Edit /etc/caddy/Caddyfile — change `support.example.com` to your domain.
sudo systemctl reload caddy
```

Caddy listens on 80/443 on the host and reverse-proxies to the frontend
container's published port (`127.0.0.1:8080`). It auto-provisions a Let's
Encrypt cert on first request.

### 7.5 Ingest + create users

```bash
# Copy your real docs in:
scp -r ./company-docs/* deploy@server:~/rag/docs/
ssh deploy@server 'cd rag && docker compose exec backend python ingest.py /app/docs'
ssh deploy@server 'cd rag && docker compose exec backend python -m auth add-user alice@company.com'
```

### 7.6 Updates

```bash
git pull && docker compose up -d --build
```

Migrations apply automatically on backend startup. Postgres data persists in
the `pgdata` volume across container restarts and rebuilds.

### 7.7 Backups

```bash
./backup.sh                          # writes ./backups/ragdb-<ts>.dump
RETENTION_DAYS=30 ./backup.sh        # override retention (default 14)
BACKUP_AGE_RECIPIENT=age1...  ./backup.sh   # encrypt with age
```

Add to root's crontab (host, not container):

```
14 3 * * * /opt/rag/backup.sh >> /var/log/rag-backup.log 2>&1
```

Restore:

```bash
docker compose exec -T postgres pg_restore --clean --if-exists \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" < backups/ragdb-YYYYMMDD....dump
```

For off-host storage (rsync to S3 / Backblaze), set `BACKUP_AGE_RECIPIENT` to
your age public key — the script will encrypt before retention runs, and the
remote copy is opaque without your private key.

---

## Security

What's enforced (mapped roughly to OWASP Top 10):

- **A01 Broken access control** — every API endpoint except `/api/login`,
  `/api/logout`, `/health` requires a valid session cookie.
- **A02 Cryptographic failures** — passwords stored as bcrypt (cost 12);
  session ids stored as sha256 (so a leaked DB doesn't yield live cookies);
  cookies HMAC-signed with `SESSION_SECRET`; cookies `HttpOnly`, `Secure`,
  `SameSite=Strict`.
- **A03 Injection** — parameterised queries everywhere (psycopg3); strict
  Pydantic validation on every request body; user input is never interpolated
  into SQL or LLM instructions. Retrieved context is wrapped in
  `<context>...</context>` and the system prompt instructs the model to
  treat it as untrusted data.
- **A04 Insecure design** — no signup, no password reset over HTTP, no admin
  routes exposed; users created by operators only.
- **A05 Security misconfiguration** — Postgres has no published port; backend
  has no published port; only the frontend's `:8080` is exposed; CSP,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, etc.; FastAPI
  docs/openapi disabled.
- **A06 Vulnerable components** — every dependency pinned to an exact version
  in `requirements.txt`; bump deliberately.
- **A07 Auth failures** — bcrypt; constant-time password compare; per-account
  lockout (10 fails / 15 min); per-IP rate limit on `/api/login` (5/min) and
  `/api/chat` (30/min); generic error messages so login responses don't
  reveal whether an email exists.
- **A08 Data integrity failures** — sessions are server-side, not JWT; no
  client-side trust boundaries to verify.
- **A09 Logging failures** — logs include `user_id` + query length, never
  query *content*; no PII in logs; tracebacks stay server-side, generic
  messages go to the client.
- **A10 SSRF** — backend only makes outbound calls to OpenRouter; the URL is
  hardcoded.

Containers run as non-root (`uid 10001` / nginx). The OpenRouter API key
lives only in the backend container's env; it never reaches the browser.

### Dependency hash-pinning (recommended)

`backend/requirements.txt` ships with version pins but no per-package
SHA256 hashes. A compromised PyPI account could re-publish the same
version with a backdoor and the next rebuild would silently pick it up.

To close that gap, generate a hashed lockfile:

```bash
pip install pip-tools==7.4.1
./scripts/lock-deps.sh
git diff backend/requirements.txt
git commit backend/requirements.in backend/requirements.txt
docker compose build --no-cache backend
```

The Dockerfile auto-detects the hash markers and switches to
`pip install --require-hashes` on next build — a tampered package now
fails the build with a checksum mismatch.

### Pre-deploy checklist (DSGVO / TMG)

Before pointing real users at the production URL, every item below must
be done — the CI `legal-placeholders` job blocks merges that still
contain bracketed placeholders, but several items live outside the repo:

1. **Fill all `[...]` placeholders** in `frontend/impressum.html`,
   `frontend/datenschutz.html`, `frontend/.well-known/security.txt`,
   and `docs-ops/vvt.md`. The CI job catches the three frontend files
   on every PR.
2. **Sign and file the Auftragsverarbeitungsvertrag (AVV)** with
   OpenRouter and any sub-processor (e.g. Anthropic, Mistral). Record
   signing date + storage location in `docs-ops/vvt.md`.
3. **Pick the LLM intentionally.** `OPENROUTER_MODEL` defaults to
   `anthropic/claude-haiku-4-5` (US, DPF-zertifiziert) for quality;
   switch to `mistralai/mistral-medium-3.1` (EU vendor, no third-country
   transfer) if the deployment is DSGVO-strict. The chosen vendor must
   match the sub-processor named in `frontend/datenschutz.html`.
4. **Complete the VVT (Art. 30)** in `docs-ops/vvt.md` and have the
   DSB / Datenschutzbeauftragte:r sign off.
5. **Run a DSFA threshold check (Art. 35).** Internal employee tool
   with chat-content storage + third-country transfer + LLM output
   plausibly crosses the threshold; document the assessment.
6. **TLS, DNS, registrar lock, DMARC/SPF/CAA** for the production
   domain. Caddy handles cert issuance; the rest is registrar/DNS-host
   work outside this repo.
7. **`.env` permissions on the VPS** must be `600` and owned by the
   docker-user, not the SSH user.
8. **Restore-drill** the `backup.sh` output once to a scratch VPS
   before relying on it.

---

## Operational notes

- Sessions are 24h. Bump `session_ttl_hours` in `config.py` if needed.
- Embedding model is local — no internet needed at query time except for the
  LLM call.
- `/health` is the only public unauth endpoint. There is no `/debug`,
  no `/version`, no `/api/openapi.json`.
- Logs go to stdout (Docker captures them). Use `docker compose logs -f
  backend` to tail.

Longer-form ops documentation lives under [`docs-ops/`](docs-ops/):

- [`log-shipping.md`](docs-ops/log-shipping.md) — wiring container logs to
  Grafana Loki.
- [`scaling-500-plus.md`](docs-ops/scaling-500-plus.md) — phased roadmap
  for growing past the default 10–20 concurrent-active capacity, with
  explicit entry triggers for each phase and a position on Kubernetes.

### Tracing (optional)

OpenTelemetry is wired but **off by default**. To ship spans to a collector:

```bash
# In .env (then docker compose up -d --force-recreate backend):
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
# Optional: also dump to backend stdout for local debugging
OTEL_CONSOLE_EXPORTER=true
```

What gets traced:

- HTTP requests (auto via `opentelemetry-instrumentation-fastapi`,
  `/health` excluded so probes don't flood the trace stream).
- Outbound httpx calls to OpenRouter.
- The chat pipeline as nested manual spans: `chat.stream` →
  `rag.retrieve` → `{rag.rewrite, rag.embed_query, rag.retrieve.dense,
  rag.retrieve.bm25, rag.rerank}` → `chat.llm.stream`. The LLM span
  records `first_token_ms` (TTFT) and `llm.tokens_streamed`.

Span attributes are metadata only (counts, scores, user_id,
allowed_tags_count, model name). User queries and document content are
**never** put on spans — same rule as logs.

---

## Layout

```
.
├── backend/
│   ├── main.py            FastAPI app: lifespan, middleware, routes
│   ├── auth.py            login, sessions, lockout, add-user CLI
│   ├── totp.py            self-service 2FA (RFC 6238) endpoints
│   ├── rag.py             retrieve top-k + stream LLM completion
│   ├── ingest.py          docs CLI: parse, chunk, embed, store
│   ├── db.py              connection pool + migration runner
│   ├── acls.py            tag normalisation + SQL visibility clause
│   ├── telemetry.py       OpenTelemetry setup (off unless env-configured)
│   ├── rate_limit.py      shared slowapi Limiter (broken into its own
│   │                      module so totp.py can use @limiter.limit)
│   ├── cleanup.py         background task: reap expired sessions /
│   │                      login_challenges every 6 h (audit L-6)
│   ├── .pip-audit-ignore  CVEs explicitly accepted in CI, with reason
│   ├── config.py          pydantic-settings, env-driven
│   ├── prompts/           Jinja2 templates + render() helper
│   │   ├── system_de.j2
│   │   ├── rewrite_de.j2
│   │   ├── user_with_context.j2
│   │   └── rewrite_user.j2
│   ├── migrations/        idempotent SQL, applied in lexical order
│   ├── tests/             pytest unit suite (no DB / no model)
│   ├── pytest.ini
│   ├── requirements.txt
│   ├── requirements-test.txt
│   └── Dockerfile
├── frontend/
│   ├── index.html · app.js · style.css
│   ├── nginx.conf         proxies /api/* to backend; serves static
│   └── Dockerfile
├── docs/                  drop your company documents here
├── docker-compose.yml
├── Caddyfile.example      reverse-proxy + HTTPS
├── smoke_test.sh
├── .env.example
└── README.md
```

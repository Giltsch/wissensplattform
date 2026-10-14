"""Centralised settings. Read once on startup; never re-read at request time.

All secrets come from the environment. Defaults here are safe-by-default
(production-ish). Local overrides go in `.env` at the repo root.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Postgres ----
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: SecretStr

    # ---- Sessions ----
    # Used for HMAC-signing the cookie value. The session id itself is random;
    # the secret guards against attackers forging a cookie with a known id.
    session_secret: SecretStr
    # Set true once you're behind HTTPS. Cookies with Secure=true are silently
    # dropped on plain HTTP, which would break local dev.
    cookie_secure: bool = False
    session_ttl_hours: int = 24

    # ---- CORS / origin ----
    allowed_origin: str = "http://localhost:8080"

    # ---- LLM ----
    # Which Chat-Completions backend to talk to. All three speak the same
    # OpenAI-compatible contract; they differ only in URL + headers (see
    # llm_provider.py). `openrouter` is the hosted gateway; `vllm`/`ollama`
    # are locally-hosted servers reached over the private docker network
    # (opt-in `llm` compose profile). A local provider keeps every chat on
    # your own infra — no OpenRouter third-country transfer (Art. 44 ff.).
    llm_provider: Literal["openrouter", "vllm", "ollama"] = "openrouter"
    # Base URL of the local OpenAI-compatible server (vllm/ollama only).
    # Examples: http://vllm:8000  or  http://ollama:11434
    # Ignored for openrouter. llm_provider.provider_endpoint() normalises a
    # trailing slash / an already-present /v1.
    llm_base_url: str = ""
    # Bearer token for the chat endpoint. Required for openrouter; usually
    # empty for local providers (vllm/ollama don't authenticate by default).
    # When empty, no Authorization header is sent.
    openrouter_api_key: SecretStr = SecretStr("")
    # DSGVO note: every chat sends the user's question + retrieved chunks
    # to this model. That is a third-country transfer (Art. 44 ff.) unless
    # the model is EU-hosted. For DSGVO-strict deployments use
    # `mistralai/mistral-medium-3.1` (EU vendor, EU-hosted).
    # For non-strict deployments `anthropic/claude-haiku-4-5` is fine
    # provided the operator has an AVV with OpenRouter, OpenRouter has
    # the standard Anthropic sub-AVV, and Anthropic's DPF certification
    # is named in the data-protection page. Either way: don't ship the
    # placeholder model name to production without a deliberate choice.
    # For a local provider (vllm/ollama) this holds the *served* model name
    # (e.g. the HuggingFace repo vLLM was launched with, or the Ollama tag),
    # not an OpenRouter slug.
    openrouter_model: str = "anthropic/claude-haiku-4-5"
    openrouter_referer: str = "http://localhost:8080"
    openrouter_title: str = "Internal Support RAG"
    # Optional secondary model used only after the primary exhausts its
    # retry budget on transient upstream errors (429/5xx). Empty string
    # disables the fallback. Set this to a different vendor's model
    # (e.g. mistralai/mistral-medium-3.1 when primary is an Anthropic
    # model) if you want resilience against a single provider's
    # rate-limit pressure. Pick a fallback in the *same* legal regime as
    # the primary so a fallback doesn't silently widen the third-country
    # transfer disclosed in datenschutz.html.
    openrouter_fallback_model: str = ""
    # On 429/502/503/504 from OpenRouter we retry the same model this
    # many *additional* times with exponential backoff before either
    # switching to the fallback model or giving up. Kept small because
    # the user is staring at a spinner during the wait.
    openrouter_max_retries: int = 2
    # Base for exponential backoff in seconds — attempt N waits
    # base * 2**N, capped at 30 s. Overridden by an upstream
    # ``Retry-After`` header if present and <= 30 s.
    openrouter_retry_base_s: float = 1.0

    # ---- Embeddings ----
    # multilingual-e5-base outputs 768-dim vectors and is trained on 100+
    # languages — required because the document corpus is German. The model
    # expects per-input prefixes ("query: ..." for queries, "passage: ..."
    # for chunks); rag.py and ingest.py apply them.
    embedding_model: str = "intfloat/multilingual-e5-base"
    # Hardcoded so a model swap forces an explicit migration of the
    # chunks.embedding column type. See migrations/003_multilingual_embeddings.sql
    # for the precedent.
    embedding_dim: int = 768

    # ---- Retrieval / reranking ----
    # We pull a wide pool with the bi-encoder, then rerank with a cross-encoder
    # for precision. Cross-encoders are slow per-pair but extremely accurate
    # at small N — perfect for a "rerank top-K" step.
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    retrieve_k: int = 20  # bi-encoder candidates fed into the reranker
    rerank_top_k: int = 5  # final chunks shown to the LLM
    # Sigmoid-normalised reranker score below this means "no good match" —
    # the LLM is asked the question with an empty context block, which the
    # system prompt routes to the standard refusal answer.
    relevance_threshold: float = 0.3

    # ---- Conversation-aware retrieval ----
    # Number of prior turns the client may send. Bounded so a malicious or
    # buggy client can't blow up the prompt budget.
    max_history_turns: int = 10
    # Total characters across all history messages, summed. Independent of
    # turn count so a few long messages don't sneak past the turn cap.
    max_history_chars: int = 8000
    # When history is non-empty, an LLM call rewrites the latest user
    # question into a standalone search query before embedding it. Hard
    # cap on the rewrite output keeps cost predictable.
    query_rewrite_enabled: bool = True
    query_rewrite_max_tokens: int = 80

    # ---- Auth / lockout ----
    bcrypt_cost: int = 12
    max_failed_logins: int = 10
    lockout_minutes: int = 15

    # AEAD key for wrapping TOTP secrets in the DB (audit C6-HIGH-6).
    # 32 random bytes, url-safe-base64-encoded. Generate once with:
    #   python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
    # If empty, the code falls back to writing the secret in plaintext
    # and logs a loud WARNING on every TOTP setup — operationally safe
    # but pre-audit posture. Rotate by setting the new value, re-running
    # the rewrap CLI (`python -m secret_box rewrap` — TODO), then
    # discarding the old key.
    totp_kms_key: SecretStr = SecretStr("")

    # ---- Per-user budget ----
    # SlowAPI gives us a per-IP cap on /api/chat, but a logged-in user behind
    # rotating IPs (or just inside a NAT) can still burn OpenRouter budget.
    # This is a per-user daily cap, enforced via the chat_usage table.
    # Generous enough that real users don't hit it; tight enough that abuse
    # caps total spend at a known multiple of the per-call cost.
    chat_daily_limit_per_user: int = 200

    # ---- External sources (URL connector) ----
    # The URL connector fetches an operator-supplied allow-list of http(s)
    # URLs (admin /api/admin/sources/sync or the ingest CLI --source url).
    # These caps live in the fetch layer (connectors/url.py), mirroring the
    # upload path's byte cap + clamd scan.
    #
    # Connect + read timeout per fetch, in seconds.
    source_fetch_timeout_s: float = 15.0
    # Max bytes accepted per fetched URL, enforced WHILE streaming the body
    # (we never trust a server Content-Length). Defaults to the same 20 MB
    # cap as the admin upload path (admin.MAX_UPLOAD_BYTES) so the two ingest
    # surfaces agree.
    source_max_bytes: int = 20 * 1024 * 1024
    # User-Agent sent on every fetch. Identifies our crawler to operators of
    # the target sites; no version string so it doesn't fingerprint the build.
    url_fetch_user_agent: str = "WissensplattformBot/1.0"

    # ---- CRM / external-DB connector (Phase 4) ----
    # OFF by default. When on, a SEPARATE one-off sync container
    # (backend/crm_sync.py + the 'crm' compose profile) batches anonymized
    # CRM records INTO the documents table — there is NO live query on the
    # chat path, and Presidio runs only in that container, never the always-on
    # backend. All credentials are SecretStr so they never land in a log or
    # repr. See SECURITY.md "CRM / external-DB connector".
    crm_enabled: bool = False
    # Connector kind. Today only 'sql' (read-only external SQL replica).
    crm_connector: str = ""
    # Read-only libpq DSN for the external CRM replica. MUST point at a role
    # with only SELECT grants — the SELECT-only query check in
    # connectors/sql_connector.py is defense-in-depth, not the primary
    # control. SecretStr so it's never logged.
    crm_dsn: SecretStr = SecretStr("")
    # HMAC key for deterministic pseudonymization of structured fields
    # (pii.pseudonymize). Rotating this changes every pseudonym, so a rotation
    # requires a FULL re-sync (old documents would otherwise read
    # inconsistently against new ones). Generate like SESSION_SECRET:
    #   openssl rand -hex 32
    pii_pseudonym_key: SecretStr = SecretStr("")
    # Row cap per sync run (LIMIT appended to the operator query) so a runaway
    # table can't OOM the sync container.
    crm_max_records_per_run: int = 5000
    # Per-record character cap on the SCRUBBED text fed to the embedder.
    # Mirrors ingest.MAX_PDF_TEXT_CHARS in spirit but far smaller — a CRM
    # record is not a 5 MB PDF.
    crm_max_record_chars: int = 20_000
    # Languages the Presidio NER backstop runs (comma-separated). de+en
    # matches the corpus + the spaCy models pinned in requirements-sync.txt.
    presidio_languages: str = "de,en"
    # Optional periodic sync interval (seconds) for a long-running sync
    # container. 0 = one-shot (the default; the container exits after one
    # pass). Mirrors the cleanup.py loop pattern.
    crm_sync_interval_s: int = 0

    # ---- ClamAV (optional malware scan on upload) ----
    # Off by default — costs ~1 GB RAM at idle. Turn on by setting
    # CLAMAV_ENABLED=true in .env and starting the clamav profile:
    #   docker compose --profile security up -d
    # When enabled, an unreachable clamd is treated as fail-closed
    # (the upload is rejected) — better to refuse than miss malware.
    clamav_enabled: bool = False
    clamav_host: str = "clamav"
    clamav_port: int = 3310
    # Per-payload timeout. ClamAV needs more time on large PDFs.
    clamav_timeout_s: float = 30.0

    # ---- Retention (DSGVO Art. 5 Abs. 1 lit. e: Speicherbegrenzung) ----
    # cleanup.py applies each window every 6 hours. A value of 0 disables
    # the sweep for that table — useful for `messages` where the
    # Datenschutz page may say "Kontolaufzeit" rather than a fixed
    # window. Operator changes must be reflected in datenschutz.html
    # AND docs-ops/vvt.md to keep the disclosure honest.
    retention_login_attempts_days: int = 90
    retention_chat_usage_days: int = 30
    retention_admin_audit_days: int = 365  # GoBD-relevant — operator may extend
    retention_messages_days: int = 0       # 0 = keep for account lifetime

    # ---- Misc ----
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("session_secret")
    @classmethod
    def _session_secret_strong(cls, v: SecretStr) -> SecretStr:
        # HMAC-signs every session cookie. A short secret weakens the
        # whole session-cookie chain — auditor flagged this because the
        # .env.example placeholder is only 28 chars (audit C3-LOW-9,
        # 2026-05-12). 32 chars is below `openssl rand -hex 32` output
        # (64 chars) but covers any "shortened on accident" mistake.
        raw = v.get_secret_value()
        if len(raw) < 32:
            raise ValueError(
                "SESSION_SECRET must be at least 32 characters; "
                "generate with: openssl rand -hex 32"
            )
        return v

    @model_validator(mode="after")
    def _validate_llm_provider(self) -> "Settings":
        # An API key is required iff we're talking to OpenRouter. Local
        # providers run on our own network and usually have no auth — but
        # they DO need a base URL to reach. Fail fast at startup rather than
        # at the first chat request.
        if self.llm_provider == "openrouter":
            if not self.openrouter_api_key.get_secret_value():
                raise ValueError(
                    "OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter"
                )
        else:
            if not self.llm_base_url.strip():
                raise ValueError(
                    f"LLM_BASE_URL is required when LLM_PROVIDER={self.llm_provider} "
                    "(e.g. http://vllm:8000 or http://ollama:11434)"
                )
        return self

    @model_validator(mode="after")
    def _validate_crm(self) -> "Settings":
        # When the CRM sync is enabled it MUST have a pseudonym key (else
        # pii.anonymize would refuse) and a connector to drive. Fail fast at
        # config load (the sync container's startup) rather than mid-run after
        # we've already opened the read-only CRM connection.
        if self.crm_enabled:
            if not self.pii_pseudonym_key.get_secret_value():
                raise ValueError(
                    "PII_PSEUDONYM_KEY is required when CRM_ENABLED=true"
                )
            if not self.crm_connector.strip():
                raise ValueError(
                    "CRM_CONNECTOR is required when CRM_ENABLED=true (e.g. 'sql')"
                )
        return self

    @property
    def presidio_language_list(self) -> list[str]:
        """presidio_languages parsed into a clean list of lang codes."""
        return [s.strip() for s in self.presidio_languages.split(",") if s.strip()]

    @property
    def dsn(self) -> str:
        # libpq-style DSN. Password is exposed only to psycopg, never logged.
        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"dbname={self.postgres_db} user={self.postgres_user} "
            f"password={self.postgres_password.get_secret_value()}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

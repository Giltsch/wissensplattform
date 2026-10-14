"""LLM provider seam.

The chat pipeline speaks the OpenAI-compatible Chat Completions contract.
Three providers share that contract and differ only in the endpoint URL and
the request headers:

- ``openrouter`` — the hosted gateway (default). Needs an API key and accepts
  the OpenRouter-specific ``HTTP-Referer`` / ``X-Title`` attribution headers.
- ``vllm`` / ``ollama`` — a locally-hosted OpenAI-compatible server reached
  over the private docker network. No third-country transfer; usually no auth.

This module is the single place that knows those differences. ``rag.py`` calls
``provider_endpoint`` / ``provider_headers`` and is otherwise provider-agnostic
(same body, same SSE parsing, same retry/fallback semantics).

Not a rate-limited route module, so ``from __future__ import annotations`` is
safe here (see CLAUDE.md — the ForwardRef caveat only applies to modules that
host ``@limiter.limit`` routes).
"""
from __future__ import annotations

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def provider_endpoint(settings) -> str:
    """Resolve the Chat Completions endpoint for the configured provider.

    For ``openrouter`` this is the fixed hosted URL. For local providers it is
    ``llm_base_url`` joined with ``/chat/completions``. We normalise a trailing
    slash and an already-present ``/v1`` so the operator can paste any of:

        http://vllm:8000
        http://vllm:8000/
        http://vllm:8000/v1
        http://vllm:8000/v1/

    and always get a single, well-formed ``.../v1/chat/completions``.
    """
    if settings.llm_provider == "openrouter":
        return OPENROUTER_URL

    base = settings.llm_base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def provider_headers(settings) -> dict:
    """Build request headers for the configured provider.

    Always sets ``Content-Type``. Adds ``Authorization: Bearer <key>`` only
    when an API key is configured (local servers usually have none). The
    OpenRouter attribution headers (``HTTP-Referer`` / ``X-Title``) are sent
    *only* for OpenRouter — local backends reject unknown headers.
    """
    headers = {"Content-Type": "application/json"}

    api_key = settings.openrouter_api_key.get_secret_value()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if settings.llm_provider == "openrouter":
        headers["HTTP-Referer"] = settings.openrouter_referer
        headers["X-Title"] = settings.openrouter_title

    return headers

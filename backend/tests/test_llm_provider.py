"""Endpoint + header matrix for the LLM provider seam.

Pure functions over a settings-like object — no HTTP, no model load, no DB.
We use a tiny stand-in instead of the real ``Settings`` so we can exercise
the vllm/ollama branches without satisfying the full env-var schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from llm_provider import OPENROUTER_URL, provider_endpoint, provider_headers


class _Secret:
    def __init__(self, value: str):
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


@dataclass
class _FakeSettings:
    llm_provider: str = "openrouter"
    llm_base_url: str = ""
    api_key: str = "sk-test"
    openrouter_referer: str = "http://localhost:8080"
    openrouter_title: str = "Internal Support RAG"
    openrouter_api_key: _Secret = field(default=None)  # type: ignore[assignment]

    def __post_init__(self):
        self.openrouter_api_key = _Secret(self.api_key)


# ---- provider_endpoint ---------------------------------------------------

def test_endpoint_openrouter_is_fixed_url():
    s = _FakeSettings(llm_provider="openrouter", llm_base_url="ignored")
    assert provider_endpoint(s) == OPENROUTER_URL


@pytest.mark.parametrize(
    "base",
    [
        "http://vllm:8000",
        "http://vllm:8000/",
        "http://vllm:8000/v1",
        "http://vllm:8000/v1/",
        "  http://vllm:8000/  ",  # surrounding whitespace
    ],
)
def test_endpoint_vllm_normalises_to_single_v1(base):
    s = _FakeSettings(llm_provider="vllm", llm_base_url=base)
    assert provider_endpoint(s) == "http://vllm:8000/v1/chat/completions"


def test_endpoint_ollama_joins_base():
    s = _FakeSettings(llm_provider="ollama", llm_base_url="http://llm:11434")
    assert provider_endpoint(s) == "http://llm:11434/v1/chat/completions"


def test_endpoint_no_double_v1():
    s = _FakeSettings(llm_provider="vllm", llm_base_url="http://llm:8000/v1")
    assert provider_endpoint(s).count("/v1") == 1


# ---- provider_headers ----------------------------------------------------

def test_headers_openrouter_full_set():
    s = _FakeSettings(llm_provider="openrouter", api_key="sk-or-abc")
    h = provider_headers(s)
    assert h["Content-Type"] == "application/json"
    assert h["Authorization"] == "Bearer sk-or-abc"
    assert h["HTTP-Referer"] == "http://localhost:8080"
    assert h["X-Title"] == "Internal Support RAG"


def test_headers_local_with_key_omits_attribution():
    s = _FakeSettings(llm_provider="vllm", llm_base_url="http://llm:8000", api_key="local-key")
    h = provider_headers(s)
    assert h["Authorization"] == "Bearer local-key"
    assert "HTTP-Referer" not in h
    assert "X-Title" not in h


def test_headers_local_no_key_omits_authorization():
    s = _FakeSettings(llm_provider="ollama", llm_base_url="http://llm:11434", api_key="")
    h = provider_headers(s)
    assert h == {"Content-Type": "application/json"}


def test_headers_openrouter_empty_key_still_no_auth():
    # provider_headers never invents an Authorization header from nothing;
    # the empty-key-on-openrouter case is rejected by the config validator,
    # not here.
    s = _FakeSettings(llm_provider="openrouter", api_key="")
    h = provider_headers(s)
    assert "Authorization" not in h
    assert "HTTP-Referer" in h  # attribution still added for openrouter

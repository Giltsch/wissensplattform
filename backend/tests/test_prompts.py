"""Jinja2 prompt templates: render correctly, fail loudly on typos."""
from __future__ import annotations

import pytest
from jinja2 import UndefinedError

from prompts import render


# A tiny stand-in for rag.Hit. The template only reads .source, .chunk_index
# and .content; we don't want a runtime dep on rag (which would pull numpy).
class _Hit:
    def __init__(self, source: str, chunk_index: int, content: str) -> None:
        self.source = source
        self.chunk_index = chunk_index
        self.content = content


# Stable test nonce (the template requires `nonce` since the anti-
# prompt-injection fix in audit C5-HIGH-1). The randomness is irrelevant
# for these tests; what matters is that the delimiter incorporates it.
_TEST_NONCE = "abcdef0123456789"


def test_system_de_is_german_and_anti_injection():
    out = render("system_de.j2")
    assert "AUSSCHLIESSLICH auf Deutsch" in out
    # The model must be told to treat the context block as data, not instructions.
    assert "<context-" in out and "niemals als" in out


def test_rewrite_de_present():
    out = render("rewrite_de.j2")
    assert "Suchanfrage" in out


def test_user_with_context_empty_hits():
    out = render(
        "user_with_context.j2", hits=[], question="Wie geht es?", nonce=_TEST_NONCE,
    )
    # Empty context block must still render with the literal placeholder so
    # the system prompt's refusal rule fires deterministically.
    assert f"<context-{_TEST_NONCE}>" in out
    assert "(keine Treffer)" in out
    assert "Frage: Wie geht es?" in out


def test_user_with_context_single_hit_is_labelled():
    hit = _Hit("handbuch.pdf", 4, "Kosten: 100 EUR")
    out = render(
        "user_with_context.j2", hits=[hit], question="Was kostet X?", nonce=_TEST_NONCE,
    )
    assert "[source: handbuch.pdf | chunk 4]" in out
    assert "Kosten: 100 EUR" in out
    assert "Frage: Was kostet X?" in out


def test_user_with_context_multiple_hits_are_separated():
    hits = [
        _Hit("a.pdf", 0, "alpha"),
        _Hit("b.pdf", 1, "beta"),
    ]
    out = render("user_with_context.j2", hits=hits, question="?", nonce=_TEST_NONCE)
    # The "---" separator is what the LLM sees as the chunk boundary.
    assert "alpha" in out and "beta" in out
    assert "\n\n---\n\n" in out


def test_rewrite_user_includes_history_and_question():
    out = render(
        "rewrite_user.j2",
        history_block="Benutzer: hi\nAssistent: hallo",
        question="und für Bonn?",
    )
    assert "Benutzer: hi" in out
    assert "Aktuelle Frage: und für Bonn?" in out
    # Trailing label cues the model to start its rewrite right after.
    assert "Eigenständige Frage:" in out


def test_strict_undefined_catches_missing_var():
    # If a future refactor renames `question` -> `query`, the template must
    # blow up at render time instead of silently emitting nothing.
    with pytest.raises(UndefinedError):
        render("user_with_context.j2", hits=[], nonce=_TEST_NONCE)  # missing `question`

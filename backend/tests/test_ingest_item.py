"""Phase 0 refactor: pin the shared ingest pipeline behaviour.

These tests run without a live DB or a real embedding model. The PDF path
is exercised with a fake ``pypdf`` module injected into ``sys.modules`` so
we never load the real (heavy) parser, and ``_insert_document_with_chunks``
plus the embedder are mocked so ``ingest_item`` can be driven end-to-end in
isolation.

They lock in the no-behaviour-change contract of the Phase 0 refactor:
parse dispatch, tag pass-through (inheritance semantics live in
``_insert_document_with_chunks``, which ``ingest_item`` must call with the
tags unchanged), and the dedup-skip / empty-document outcomes.
"""
from __future__ import annotations

import sys
import types

import pytest

import ingest
from ingest import EmptyDocumentError, ParseError, SourceItem, ingest_item, parse_bytes


# ---- parse_bytes dispatch ------------------------------------------------

def _install_fake_pypdf(monkeypatch, pages_text, *, page_count=None):
    """Install a fake ``pypdf`` module whose PdfReader yields the given
    page texts. ``page_count`` overrides len(reader.pages) so the page-cap
    branch can be exercised without allocating real pages."""

    class _FakePage:
        def __init__(self, txt):
            self._txt = txt

        def extract_text(self):
            return self._txt

    class _FakePages(list):
        def __init__(self, items, count):
            super().__init__(items)
            self._count = count

        def __len__(self):
            return self._count if self._count is not None else super().__len__()

    class _FakeReader:
        def __init__(self, _stream):
            items = [_FakePage(t) for t in pages_text]
            self.pages = _FakePages(items, page_count)

    fake_mod = types.ModuleType("pypdf")
    fake_mod.PdfReader = _FakeReader
    monkeypatch.setitem(sys.modules, "pypdf", fake_mod)


def test_parse_bytes_pdf_by_content_type(monkeypatch):
    _install_fake_pypdf(monkeypatch, ["Hallo", "Welt"])
    out = parse_bytes("noextension", b"%PDF-fake", "application/pdf")
    assert out == "Hallo\n\nWelt"


def test_parse_bytes_pdf_by_suffix(monkeypatch):
    _install_fake_pypdf(monkeypatch, ["Seite eins"])
    # Empty content-type -> dispatch falls back to the .pdf suffix.
    out = parse_bytes("doc.pdf", b"%PDF-fake", "")
    assert out == "Seite eins"


def test_parse_bytes_pdf_content_type_with_charset(monkeypatch):
    _install_fake_pypdf(monkeypatch, ["x"])
    out = parse_bytes("doc", b"%PDF-fake", "application/pdf; charset=binary")
    assert out == "x"


def test_parse_bytes_pdf_page_cap(monkeypatch):
    _install_fake_pypdf(monkeypatch, ["a"], page_count=ingest.MAX_PDF_PAGES + 1)
    with pytest.raises(ValueError) as ei:
        parse_bytes("big.pdf", b"%PDF-fake", "application/pdf")
    assert "pages" in str(ei.value)


def test_parse_bytes_pdf_text_cap(monkeypatch):
    huge = "x" * (ingest.MAX_PDF_TEXT_CHARS + 1)
    _install_fake_pypdf(monkeypatch, [huge])
    with pytest.raises(ValueError) as ei:
        parse_bytes("big.pdf", b"%PDF-fake", "application/pdf")
    assert "MB" in str(ei.value)


def test_parse_bytes_md_is_utf8_decoded():
    out = parse_bytes("notes.md", "# Überschrift".encode("utf-8"), "text/markdown")
    assert out == "# Überschrift"


def test_parse_bytes_txt_is_utf8_decoded():
    out = parse_bytes("plain.txt", b"just text", "text/plain")
    assert out == "just text"


def test_parse_bytes_unknown_suffix_falls_back_to_text():
    # Mirrors the prior admin._parse_payload behaviour: any non-pdf input is
    # decoded as utf-8 rather than rejected at the parse layer (the suffix
    # allow-list is enforced earlier in the upload handler / iter_files).
    out = parse_bytes("data.bin", b"raw bytes", "")
    assert out == "raw bytes"


def test_parse_bytes_invalid_utf8_is_replaced():
    out = parse_bytes("weird.txt", b"\xff\xfe abc", "text/plain")
    # errors="replace" -> no exception, replacement chars present.
    assert "abc" in out


# ---- ingest_item: dedup skip ---------------------------------------------

async def test_ingest_item_dedup_skips_before_parse_or_embed(monkeypatch):
    """If the file hash is already ingested, ingest_item returns None
    without parsing, embedding, or attempting an insert."""
    monkeypatch.setattr(
        ingest, "_document_already_ingested", _async_return(True)
    )

    parse_calls = []
    monkeypatch.setattr(
        ingest, "parse_bytes",
        lambda *a, **k: parse_calls.append(a) or "should not be used",
    )
    insert_calls = []
    monkeypatch.setattr(
        ingest, "_insert_document_with_chunks",
        _record_async(insert_calls),
    )

    embedder = _FakeEmbedder()
    item = SourceItem(source="dup.txt", raw_bytes=b"content", content_type="text/plain")
    result = await ingest_item(item, embedder, tags=None)

    assert result is None
    assert parse_calls == []
    assert embedder.encode_calls == 0
    assert insert_calls == []


# ---- ingest_item: empty document -----------------------------------------

async def test_ingest_item_raises_on_empty_document(monkeypatch):
    monkeypatch.setattr(
        ingest, "_document_already_ingested", _async_return(False)
    )
    # Whitespace-only text -> chunk_text returns [] -> EmptyDocumentError.
    item = SourceItem(source="empty.txt", raw_bytes=b"   \n\n   ", content_type="text/plain")
    with pytest.raises(EmptyDocumentError):
        await ingest_item(item, _FakeEmbedder(), tags=None)


# ---- ingest_item: parse failure normalised to ParseError -----------------

async def test_ingest_item_wraps_parse_failure_as_parse_error(monkeypatch):
    """A parse-layer exception (e.g. the page cap) must surface as
    ParseError so the admin path can 400 it without swallowing DB errors."""
    monkeypatch.setattr(
        ingest, "_document_already_ingested", _async_return(False)
    )
    _install_fake_pypdf(monkeypatch, ["a"], page_count=ingest.MAX_PDF_PAGES + 1)
    item = SourceItem(source="big.pdf", raw_bytes=b"%PDF", content_type="application/pdf")
    with pytest.raises(ParseError):
        await ingest_item(item, _FakeEmbedder(), tags=None)


# ---- ingest_item: tag pass-through ---------------------------------------

@pytest.mark.parametrize("tags", [None, [], ["finanzen"], ["a", "b"]])
async def test_ingest_item_forwards_tags_verbatim(monkeypatch, tags):
    """The inheritance/public semantics live in _insert_document_with_chunks;
    ingest_item must hand the caller's tags through unchanged so that
    contract is preserved (None -> inherit/public, [] -> public, [...] -> set)."""
    monkeypatch.setattr(
        ingest, "_document_already_ingested", _async_return(False)
    )
    captured = {}

    async def fake_insert(file_hash, source, chunks, hashes, embeddings,
                          tags=None, origin="local",
                          source_type="file", source_meta=None):
        captured["file_hash"] = file_hash
        captured["source"] = source
        captured["tags"] = tags
        captured["origin"] = origin
        captured["n_chunks"] = len(chunks)
        return (1, len(chunks), 1, None)

    monkeypatch.setattr(ingest, "_insert_document_with_chunks", fake_insert)

    item = SourceItem(
        source="real.txt",
        raw_bytes=b"one two three four five",
        content_type="text/plain",
    )
    result = await ingest_item(item, _FakeEmbedder(), tags=tags, origin="url")

    assert result == (1, captured["n_chunks"], 1, None)
    # tags object identity/value preserved (None stays None, [] stays []).
    assert captured["tags"] == tags
    assert captured["source"] == "real.txt"
    # origin is threaded verbatim to the insert (migration 015).
    assert captured["origin"] == "url"
    # file_hash is sha256 over the raw bytes (dedup key invariant).
    import hashlib
    assert captured["file_hash"] == hashlib.sha256(item.raw_bytes).hexdigest()


async def test_ingest_item_defaults_origin_local(monkeypatch):
    """Callers that don't pass origin get 'local', so the pre-Phase-3 local
    sync and admin upload paths are unchanged."""
    monkeypatch.setattr(
        ingest, "_document_already_ingested", _async_return(False)
    )
    captured = {}

    async def fake_insert(file_hash, source, chunks, hashes, embeddings,
                          tags=None, origin="local",
                          source_type="file", source_meta=None):
        captured["origin"] = origin
        return (1, len(chunks), 1, None)

    monkeypatch.setattr(ingest, "_insert_document_with_chunks", fake_insert)
    item = SourceItem(
        source="a.txt", raw_bytes=b"one two three", content_type="text/plain",
    )
    await ingest_item(item, _FakeEmbedder(), tags=None)
    assert captured["origin"] == "local"


# ---- helpers -------------------------------------------------------------

class _FakeEmbedder:
    """Stand-in for ingest.Embedder — no model load."""

    def __init__(self):
        self.encode_calls = 0

    def encode(self, texts):
        self.encode_calls += 1
        # Shape doesn't matter; the mocked insert ignores it.
        return [[0.0] for _ in texts]


def _async_return(value):
    async def _coro(*_a, **_k):
        return value
    return _coro


def _record_async(sink):
    async def _coro(*a, **k):
        sink.append((a, k))
        return None
    return _coro

"""Filename and PDF caps for the admin upload path.

These pin the regex / cap contracts that keep `_parse_payload` and the
upload handler from accepting abusive inputs (audit M-3, M-4, 2026-05-10).
"""
from __future__ import annotations

import pytest

from admin import _BACKUP_SUFFIX_RE, _SAFE_NAME, MAX_PDF_PAGES, MAX_PDF_TEXT_CHARS


# ---- _SAFE_NAME (M-3) --------------------------------------------------

@pytest.mark.parametrize("name", [
    "handbook.pdf",
    "policy_v2.md",
    "with-dash.txt",
    "a.b.c.md",      # multiple dots OK
    "1page.pdf",     # leading digit OK
    "x.pdf",
])
def test_safe_name_accepts_normal_filenames(name):
    assert _SAFE_NAME.match(name)


@pytest.mark.parametrize("name", [
    "..pdf",         # leading dot — would be ".." followed by ".pdf"
    "...pdf",
    ".env",
    ".env.txt",      # hidden file
    "-rf",           # starts with dash — could surprise CLI tools
    "-rm-rf-slash",
    "../etc/passwd", # path traversal (Path().name strips this anyway, but defence-in-depth)
    "evil/file.pdf",
    "with space.pdf",
    "umlaut-ä.pdf",  # non-ascii
    "file?.pdf",     # shell meta
    "file;rm.pdf",
    "",
])
def test_safe_name_rejects_dangerous_filenames(name):
    assert not _SAFE_NAME.match(name)


# ---- _BACKUP_SUFFIX_RE (M-3) -------------------------------------------

@pytest.mark.parametrize("name", [
    "doc.v1.bak",
    "doc.v12.bak",
    "handbook.pdf.v1.bak",   # the actual shape the upload handler writes
    "weird.v999.bak",
])
def test_backup_suffix_blocks_collision_with_archive_naming(name):
    assert _BACKUP_SUFFIX_RE.search(name)


@pytest.mark.parametrize("name", [
    "doc.bak",         # missing .vN
    "doc.v.bak",       # no digit
    "doc.v1.txt",      # not .bak
    "doc.v1bak",       # missing dot before "bak"
    "v1.bak",          # no leading dot before v1 → not the archive shape
])
def test_backup_suffix_does_not_match_unrelated_names(name):
    assert not _BACKUP_SUFFIX_RE.search(name)


# ---- PDF caps (M-4) ----------------------------------------------------

def test_pdf_caps_are_set():
    # Cheap pin: if someone bumps these by accident in a code search-and-replace,
    # the test fails and they have to look at the audit context.
    assert MAX_PDF_PAGES == 500
    assert MAX_PDF_TEXT_CHARS == 5_000_000

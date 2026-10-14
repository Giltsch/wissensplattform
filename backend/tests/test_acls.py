"""Tag normalisation + visibility-clause builder.

These are the load-bearing security primitives for migration 011 — a bug
in either is a data leak. The SQL clause itself can't run without a DB,
but we can pin its shape so future refactors don't silently break the
filter contract.
"""
from __future__ import annotations

import pytest

from acls import normalise_tag, normalise_tags, visibility_clause


# ---- normalise_tag -----------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("hr", "hr"),
    ("HR", "hr"),
    ("  Finance  ", "finance"),
    ("legal_team", "legal_team"),
    ("r-and-d", "r-and-d"),
    ("a", "a"),
    ("0", "0"),
    ("ab12", "ab12"),
])
def test_normalise_tag_accepts_and_canonicalises(raw, expected):
    assert normalise_tag(raw) == expected


@pytest.mark.parametrize("bad", [
    "",                 # empty
    "   ",              # whitespace only
    "-leading-dash",    # must start [a-z0-9]
    "_leading_under",   # must start [a-z0-9]
    "with space",       # no internal spaces
    "umlautü",          # ascii only — keeps logs / audit rows clean
    "tag!",             # punctuation
    "x" * 33,           # over 32 chars
])
def test_normalise_tag_rejects_invalid(bad):
    with pytest.raises(ValueError):
        normalise_tag(bad)


def test_normalise_tag_rejects_non_string():
    # The Pydantic layer should already enforce str, but defence in depth.
    with pytest.raises(ValueError):
        normalise_tag(42)  # type: ignore[arg-type]


def test_normalise_tag_idempotent():
    canon = normalise_tag("HR")
    assert normalise_tag(canon) == canon


# ---- normalise_tags (list) ---------------------------------------------

def test_normalise_tags_dedupes_after_canonicalisation():
    # 'HR' and 'hr' collapse — admins editing in the UI shouldn't have to
    # care about case.
    assert normalise_tags(["HR", "hr", "Finance"]) == ["hr", "finance"]


def test_normalise_tags_preserves_first_occurrence_order():
    assert normalise_tags(["finance", "hr", "finance"]) == ["finance", "hr"]


def test_normalise_tags_propagates_invalid():
    with pytest.raises(ValueError):
        normalise_tags(["hr", "with space"])


def test_normalise_tags_empty_input():
    assert normalise_tags([]) == []


# ---- visibility_clause -------------------------------------------------

def test_admin_clause_is_literal_true_no_params():
    sql, params = visibility_clause(is_admin=True, allowed_tags=["ignored"])
    # An admin clause must bind zero params — otherwise call sites that
    # extend their param tuple will get a column-count mismatch.
    assert sql == "TRUE"
    assert params == []


def test_non_admin_clause_uses_overlap_and_empty_check():
    sql, params = visibility_clause(is_admin=False, allowed_tags=["hr"])
    # Both halves of the visibility rule must be present: the public-doc
    # case (cardinality = 0) and the overlap case (&&).
    assert "cardinality(d.tags) = 0" in sql
    assert "d.tags && %s" in sql
    # Exactly one bound param: the user's allowed_tags array, passed
    # through to the && operator.
    assert params == [["hr"]]


def test_non_admin_clause_with_empty_allowed_tags_still_returns_publics():
    # A user with no granted tags should still see public docs. The SQL
    # fragment is the same; the && half just won't match anything.
    sql, params = visibility_clause(is_admin=False, allowed_tags=[])
    assert "cardinality(d.tags) = 0" in sql
    assert params == [[]]


def test_clause_respects_doc_alias():
    # We sometimes alias documents differently — the helper has to cope.
    sql, _ = visibility_clause(is_admin=False, allowed_tags=["hr"], doc_alias="docs")
    assert "cardinality(docs.tags) = 0" in sql
    assert "docs.tags && %s" in sql
    # And it should NOT leak the default alias.
    assert "d.tags" not in sql

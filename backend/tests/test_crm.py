"""Pure-function tests for the CRM / PII pipeline (Phase 4; no DB, no model).

Covers, unconditionally (no Presidio needed):
  * pii.pseudonymize determinism + key sensitivity,
  * pii.anonymize structured-field actions (drop / pseudonymize / keep),
  * the max_chars cap,
  * record_to_tags fail-closed mapping (unknown/empty crm_group),
  * crm_sync._scrubbed_hash idempotency (scrubbed text -> stable hash),
  * connectors.sql_connector.assert_select_only.

If Presidio IS installed, an extra test asserts the NER backstop redacts a
known fixture; it is skipped otherwise (the backstop lives only in the sync
container). The structured-field + fail-closed + pseudonym logic is asserted
in every environment.
"""
from __future__ import annotations

import importlib.util

import pytest

from connectors.base import (
    CRM_UNMAPPED_TAG,
    ExternalRecord,
    PiiAction,
    StructuredField,
    record_to_tags,
)
from connectors.sql_connector import assert_select_only
import pii


_HAS_PRESIDIO = importlib.util.find_spec("presidio_analyzer") is not None

_KEY = "test-pseudonym-key-not-for-production"


# ---- pseudonymize ---------------------------------------------------------

def test_pseudonymize_deterministic():
    a = pii.pseudonymize("kunde-123", pseudonym_key=_KEY)
    b = pii.pseudonymize("kunde-123", pseudonym_key=_KEY)
    assert a == b
    assert len(a) == 12
    # Not the raw value, and not obviously reversible.
    assert "kunde-123" not in a


def test_pseudonymize_key_sensitive():
    a = pii.pseudonymize("kunde-123", pseudonym_key=_KEY)
    b = pii.pseudonymize("kunde-123", pseudonym_key="a-different-key-entirely")
    assert a != b


def test_pseudonymize_distinct_inputs():
    a = pii.pseudonymize("alice", pseudonym_key=_KEY)
    b = pii.pseudonymize("bob", pseudonym_key=_KEY)
    assert a != b


# ---- anonymize: structured-field actions ---------------------------------

def _record(fields, free_text="", group="vertrieb", source_id="cust-1"):
    return ExternalRecord(
        source_id=source_id,
        crm_group=group,
        structured_fields=fields,
        free_text=free_text,
    )


def test_anonymize_drop_omits_field():
    rec = _record([
        StructuredField("Email", "alice@example.com", PiiAction.DROP),
        StructuredField("Status", "offen", PiiAction.KEEP),
    ])
    out = pii.anonymize(rec, pseudonym_key=_KEY, languages=["de"], max_chars=10000)
    assert "alice@example.com" not in out.text
    assert "Email" not in out.text
    assert "Status: offen" in out.text


def test_anonymize_pseudonymize_replaces_value():
    rec = _record([
        StructuredField("Kunde", "alice@example.com", PiiAction.PSEUDONYMIZE),
    ])
    out = pii.anonymize(rec, pseudonym_key=_KEY, languages=["de"], max_chars=10000)
    assert "alice@example.com" not in out.text
    token = pii.pseudonymize("alice@example.com", pseudonym_key=_KEY)
    assert token in out.text
    assert out.text.startswith("Kunde: ")


def test_anonymize_keep_verbatim():
    rec = _record([StructuredField("Produkt", "Tarif Gold", PiiAction.KEEP)])
    out = pii.anonymize(rec, pseudonym_key=_KEY, languages=["de"], max_chars=10000)
    assert "Produkt: Tarif Gold" in out.text


def test_anonymize_counts_redactions():
    rec = _record([
        StructuredField("Email", "a@b.de", PiiAction.DROP),
        StructuredField("Kunde", "x", PiiAction.PSEUDONYMIZE),
        StructuredField("Status", "offen", PiiAction.KEEP),
    ])
    out = pii.anonymize(rec, pseudonym_key=_KEY, languages=["de"], max_chars=10000)
    # DROP + PSEUDONYMIZE count; KEEP does not. (Plus any free-text spans, but
    # free_text is empty here.)
    assert out.redactions == 2


def test_anonymize_char_cap():
    rec = _record([StructuredField("Notiz", "x" * 500, PiiAction.KEEP)])
    out = pii.anonymize(rec, pseudonym_key=_KEY, languages=["de"], max_chars=50)
    assert len(out.text) == 50


def test_anonymize_requires_key():
    rec = _record([StructuredField("Status", "offen", PiiAction.KEEP)])
    with pytest.raises(ValueError):
        pii.anonymize(rec, pseudonym_key="", languages=["de"], max_chars=100)


def test_anonymize_empty_pseudonymize_marker():
    rec = _record([StructuredField("Kunde", "", PiiAction.PSEUDONYMIZE)])
    out = pii.anonymize(rec, pseudonym_key=_KEY, languages=["de"], max_chars=10000)
    assert "Kunde: <leer>" in out.text


@pytest.mark.skipif(_HAS_PRESIDIO, reason="presidio installed: free text is NER-scrubbed, not dropped")
def test_anonymize_drops_free_text_without_presidio():
    # Fail-closed: with no NER engine we must NOT emit un-scrubbed free text.
    rec = _record(
        [StructuredField("Status", "offen", PiiAction.KEEP)],
        free_text="Anruf von Herrn Schmidt unter 0151 1234567.",
    )
    out = pii.anonymize(rec, pseudonym_key=_KEY, languages=["de"], max_chars=10000)
    assert "Schmidt" not in out.text
    assert "0151 1234567" not in out.text
    assert "Status: offen" in out.text


@pytest.mark.skipif(not _HAS_PRESIDIO, reason="presidio not installed")
def test_anonymize_ner_backstop_redacts():
    rec = _record(
        [StructuredField("Status", "offen", PiiAction.KEEP)],
        free_text="Contact Alice Johnson at alice@example.com.",
    )
    out = pii.anonymize(rec, pseudonym_key=_KEY, languages=["en"], max_chars=10000)
    assert "alice@example.com" not in out.text
    assert "<EMAIL_ADDRESS>" in out.text


# ---- record_to_tags: fail-closed -----------------------------------------

_GROUP_MAP = {"Vertrieb": "vertrieb", "Support": "support"}


def test_record_to_tags_known_group():
    rec = _record([], group="Vertrieb")
    assert record_to_tags(rec, _GROUP_MAP) == ["vertrieb"]


def test_record_to_tags_case_insensitive_group():
    rec = _record([], group="vertrieb")
    assert record_to_tags(rec, _GROUP_MAP) == ["vertrieb"]


def test_record_to_tags_unknown_group_fails_closed():
    rec = _record([], group="GeheimesTeam")
    assert record_to_tags(rec, _GROUP_MAP) == [CRM_UNMAPPED_TAG]


def test_record_to_tags_empty_group_fails_closed():
    rec = _record([], group="")
    assert record_to_tags(rec, _GROUP_MAP) == [CRM_UNMAPPED_TAG]


def test_record_to_tags_never_public():
    # Whatever happens, never [] (public) and never an unmapped arbitrary tag.
    for group in ("", "   ", "Unknown", "Vertrieb"):
        rec = _record([], group=group)
        tags = record_to_tags(rec, _GROUP_MAP)
        assert tags != []
        assert len(tags) == 1


# ---- scrubbed-hash idempotency -------------------------------------------

def test_scrubbed_hash_idempotent():
    from crm_sync import _scrubbed_hash

    a = _scrubbed_hash("Status: offen\nKunde: ab12cd34ef56")
    b = _scrubbed_hash("Status: offen\nKunde: ab12cd34ef56")
    assert a == b
    assert _scrubbed_hash("Status: zu") != a


def test_anonymize_then_hash_stable_across_volatile_dropped_field():
    # A volatile field declared DROP must not influence the scrubbed text, so
    # the hash is stable even when that field changes upstream.
    from crm_sync import _scrubbed_hash
    from ingest import normalize

    base = [
        StructuredField("Status", "offen", PiiAction.KEEP),
        StructuredField("Kunde", "alice@example.com", PiiAction.PSEUDONYMIZE),
    ]
    rec1 = _record(base + [StructuredField("LastContact", "2026-01-01", PiiAction.DROP)])
    rec2 = _record(base + [StructuredField("LastContact", "2026-06-01", PiiAction.DROP)])
    t1 = normalize(pii.anonymize(rec1, pseudonym_key=_KEY, languages=["de"], max_chars=10000).text)
    t2 = normalize(pii.anonymize(rec2, pseudonym_key=_KEY, languages=["de"], max_chars=10000).text)
    assert _scrubbed_hash(t1) == _scrubbed_hash(t2)


# ---- SELECT-only guard ----------------------------------------------------

def test_select_only_accepts_select():
    assert_select_only("SELECT id, name FROM customers")


def test_select_only_accepts_cte():
    assert_select_only("WITH x AS (SELECT 1) SELECT * FROM x")


def test_select_only_rejects_update():
    with pytest.raises(ValueError):
        assert_select_only("UPDATE customers SET name = 'x'")


def test_select_only_rejects_statement_stacking():
    with pytest.raises(ValueError):
        assert_select_only("SELECT 1; DROP TABLE customers")


def test_select_only_rejects_hidden_keyword_in_comment_bypass():
    # A forbidden keyword in the live SQL is rejected even with a comment.
    with pytest.raises(ValueError):
        assert_select_only("SELECT 1 /* ok */ ; DELETE FROM t")


def test_select_only_rejects_empty():
    with pytest.raises(ValueError):
        assert_select_only("   ")

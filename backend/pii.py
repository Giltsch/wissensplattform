"""PII anonymization for the CRM sync (Phase 4).

``anonymize(record, ...)`` turns an :class:`connectors.base.ExternalRecord`
into a scrubbed text block that is safe to hash, embed, store, and later send
to the LLM. It is the scrub-before-embed invariant in code: raw identifiers
never reach the documents table, the vector store, or the chat path.

Two layers, applied in order:

  1. **Declared structured-field actions (the real guarantee).** Each
     ``StructuredField`` carries an operator-declared :class:`PiiAction`:

       * ``DROP``        — the field is omitted from the output entirely.
       * ``PSEUDONYMIZE`` — the value is replaced with a deterministic token
         ``hmac_sha256(value, pseudonym_key)[:12]``. Deterministic so the
         same customer reads consistently across records and runs (and so
         re-syncing identical data doesn't churn document versions), while
         the raw value is never recoverable from the token. Empty values map
         to a fixed ``<leer>`` marker rather than hashing the empty string.
       * ``KEEP``        — emitted verbatim (operator-confirmed non-personal).

  2. **Presidio NER backstop (defense-in-depth).** Free text is unstructured
     prose where an operator can't declare per-column actions, so a Named
     Entity Recognition pass detects PERSON / EMAIL_ADDRESS / PHONE_NUMBER /
     IBAN_CODE / LOCATION (+ a German ID recognizer) and replaces each span
     with a type token (``<PERSON>``, ``<EMAIL_ADDRESS>``, ...). Presidio is
     imported lazily so the always-on backend image never needs it — it is
     pinned only in the sync container (requirements-sync.txt). If Presidio
     is unavailable, the free text is dropped entirely (fail-closed: we never
     emit un-scrubbed free text just because the NER engine is missing).

The whole result is hard-capped at ``max_chars``. Returns scrubbed text only
— no structure, no raw values.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from connectors.base import ExternalRecord, PiiAction

log = logging.getLogger(__name__)

# Entities the Presidio backstop scrubs from free text. Kept narrow and
# explicit so the token set is predictable; LOCATION is included because CRM
# notes often name addresses/cities. A German national-id recognizer is added
# at runtime (see _build_analyzer).
_PRESIDIO_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "IBAN_CODE",
    "LOCATION",
    "DE_ID",
]

_PSEUDONYM_LEN = 12
_EMPTY_MARKER = "<leer>"


def pseudonymize(value: str, *, pseudonym_key: str) -> str:
    """Deterministic, non-reversible pseudonym for one value.

    ``hmac_sha256(value, pseudonym_key)[:12]`` — keyed so the mapping can't be
    brute-forced from the token without the key, deterministic so the same
    input always yields the same token. Rotating ``pseudonym_key`` changes
    every token, which is why a key rotation requires a full re-sync (the old
    documents would otherwise read inconsistently against new ones).
    """
    digest = hmac.new(
        pseudonym_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:_PSEUDONYM_LEN]


def _apply_structured(record: ExternalRecord, *, pseudonym_key: str) -> list[str]:
    """Render the structured fields per their declared action. Returns a list
    of ``"Label: value"`` lines (DROP fields omitted)."""
    lines: list[str] = []
    for fld in record.structured_fields:
        if fld.action == PiiAction.DROP:
            continue
        if fld.action == PiiAction.PSEUDONYMIZE:
            value = pseudonymize(fld.value, pseudonym_key=pseudonym_key) \
                if fld.value else _EMPTY_MARKER
        else:  # KEEP
            value = fld.value
        lines.append(f"{fld.label}: {value}")
    return lines


# ---- Presidio backstop ----------------------------------------------------

# Module-level cache so the spaCy models load once per process, not per
# record. None means "not yet attempted"; False means "attempted and
# unavailable" (so we don't re-import on every call).
_analyzer = None
_anonymizer = None
_presidio_state = None  # None=untried, True=ready, False=unavailable


def _build_engines(languages: list[str]):
    """Build (analyzer, anonymizer) or return (None, None) if Presidio isn't
    installed. Registers a German national-id pattern recognizer as DE_ID."""
    global _analyzer, _anonymizer, _presidio_state
    if _presidio_state is True:
        return _analyzer, _anonymizer
    if _presidio_state is False:
        return None, None
    try:
        from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine
    except Exception:
        log.warning(
            "presidio not installed; CRM free-text NER backstop is OFF "
            "(free text will be dropped). Install requirements-sync.txt in "
            "the sync container."
        )
        _presidio_state = False
        return None, None

    # One spaCy model per configured language. The sync container pins the
    # de_core_news_lg + en_core_web_lg models (see requirements-sync.txt).
    models = []
    lang_model = {"de": "de_core_news_lg", "en": "en_core_web_lg"}
    for lang in languages:
        models.append({"lang_code": lang, "model_name": lang_model.get(lang, "en_core_web_lg")})
    provider = NlpEngineProvider(
        nlp_configuration={"nlp_engine_name": "spacy", "models": models}
    )
    nlp_engine = provider.create_engine()
    analyzer = AnalyzerEngine(
        nlp_engine=nlp_engine, supported_languages=languages
    )
    # German national identifiers (Steuer-ID / Personalausweis-style 9-11
    # digit runs). Coarse on purpose — it's a backstop, not the guarantee.
    de_id = PatternRecognizer(
        supported_entity="DE_ID",
        supported_language="de",
        patterns=[Pattern(name="de_id_num", regex=r"\b\d{9,11}\b", score=0.4)],
    )
    analyzer.registry.add_recognizer(de_id)

    _analyzer = analyzer
    _anonymizer = AnonymizerEngine()
    _presidio_state = True
    return _analyzer, _anonymizer


def _scrub_free_text(text: str, *, languages: list[str]) -> tuple[str, int]:
    """Run the Presidio NER backstop over ``text``. Returns (scrubbed_text,
    redaction_count). If Presidio is unavailable, returns ('', 0) — fail
    closed: we never emit un-scrubbed free text.

    Each language's analyzer runs; detected spans across all languages are
    merged and replaced with their entity-type token (``<PERSON>`` etc.)."""
    if not text.strip():
        return "", 0
    analyzer, anonymizer = _build_engines(languages)
    if analyzer is None or anonymizer is None:
        # Fail-closed: drop free text rather than emit it un-scrubbed.
        return "", 0

    from presidio_anonymizer.entities import OperatorConfig

    # Collect results across all configured languages so an entity the
    # German model misses but the English one catches is still scrubbed.
    results = []
    for lang in languages:
        try:
            results.extend(
                analyzer.analyze(text=text, language=lang, entities=_PRESIDIO_ENTITIES)
            )
        except Exception:
            log.exception("presidio analyze failed for language=%s", lang)
    if not results:
        return text, 0

    operators = {
        ent: OperatorConfig("replace", {"new_value": f"<{ent}>"})
        for ent in _PRESIDIO_ENTITIES
    }
    operators["DEFAULT"] = OperatorConfig("replace", {"new_value": "<REDACTED>"})
    anonymized = anonymizer.anonymize(
        text=text, analyzer_results=results, operators=operators
    )
    return anonymized.text, len(results)


# ---- Public API -----------------------------------------------------------

class AnonymizeResult:
    """Scrubbed text plus a redaction count (for counts-only audit). The
    count is the number of structured fields dropped/pseudonymized plus the
    number of free-text NER spans replaced — a metric, never a value."""

    __slots__ = ("text", "redactions")

    def __init__(self, text: str, redactions: int) -> None:
        self.text = text
        self.redactions = redactions


def anonymize(
    record: ExternalRecord,
    *,
    pseudonym_key: str,
    languages: list[str],
    max_chars: int,
) -> AnonymizeResult:
    """Anonymize one ``ExternalRecord`` into scrubbed text.

    Structured-field actions are applied first (the real guarantee), then the
    Presidio NER backstop scrubs the free text. The combined output is capped
    at ``max_chars``. Returns an :class:`AnonymizeResult` (scrubbed text +
    counts-only redaction metric) — never the raw values.
    """
    if not pseudonym_key:
        # Defensive: the config validator already requires this when
        # crm_enabled, but never silently emit raw values if it's missing.
        raise ValueError("pseudonym_key is required for anonymize()")

    structured_lines = _apply_structured(record, pseudonym_key=pseudonym_key)
    # redactions: fields that were dropped or pseudonymized (KEEP isn't a
    # redaction). Counts only — never the values.
    structured_redactions = sum(
        1 for f in record.structured_fields
        if f.action in (PiiAction.DROP, PiiAction.PSEUDONYMIZE)
    )

    scrubbed_free, free_redactions = _scrub_free_text(
        record.free_text, languages=languages
    )

    parts = list(structured_lines)
    if scrubbed_free:
        parts.append(scrubbed_free)
    text = "\n".join(parts)[:max_chars]

    return AnonymizeResult(text, structured_redactions + free_redactions)

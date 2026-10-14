"""External-record connector base types (Phase 4: CRM / external DB).

A ``SourceConnector`` (see ``connectors/__init__.py``) yields raw bytes for
the *document* ingest pipeline. The CRM connector is different: it yields
*structured records* that must be anonymized BEFORE any text is hashed,
embedded, or stored. ``ExternalRecord`` is the richer envelope for that path,
and ``record_to_tags`` is the fail-closed contract that maps a record's
``crm_group`` to a department tag.

Design invariants (see SECURITY.md, "CRM / external-DB connector"):

  * The ``source_id`` is a STABLE, NON-PII identifier (e.g. an opaque
    customer key). It is the versioning key for the resulting document and
    is written into ``documents.source`` as ``crm:sql:<source_id>`` — so it
    MUST NOT be an email, name, or other identifier.
  * ``structured_fields`` carries the per-column anonymization decision the
    operator made when authoring the connector query: each field declares a
    ``PiiAction`` (drop / pseudonymize / keep). This declaration is the real
    privacy guarantee; the Presidio NER pass in ``pii.anonymize`` is only a
    backstop for free-text leakage.
  * ``free_text`` is unstructured prose (notes, descriptions) that gets the
    NER backstop.
  * ``crm_group`` is the record's grouping key (segment / team / tenant) that
    ``record_to_tags`` maps to a department. An unknown/empty group maps to
    the fail-closed ``crm_unmapped`` department (granted to nobody), NEVER to
    public (``[]``) and NEVER to an arbitrary tag.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# The fail-closed landing department (migration 016). A record whose
# crm_group can't be resolved lands here; the operator must never grant this
# tag to a user, so the record is admin-only until explicitly mapped.
CRM_UNMAPPED_TAG = "crm_unmapped"


class PiiAction(str, Enum):
    """What ``pii.anonymize`` does with a structured field's value.

    * ``DROP`` — the value never reaches the scrubbed text at all.
    * ``PSEUDONYMIZE`` — replaced with a deterministic 12-hex-char token
      (``hmac_sha256(value, pseudonym_key)[:12]``) so the same customer
      reads consistently across records/runs without exposing the raw value.
    * ``KEEP`` — emitted verbatim. Use ONLY for fields the operator has
      confirmed are non-personal (e.g. a product category, a ticket status).
    """

    DROP = "drop"
    PSEUDONYMIZE = "pseudonymize"
    KEEP = "keep"


@dataclass
class StructuredField:
    """One column of a CRM record plus its declared anonymization action.

    ``label`` is the human-readable field name emitted into the scrubbed
    text (e.g. "Status"); it is operator-authored metadata, not record data,
    so it is always safe to keep. ``value`` is the raw cell value (stringified
    by the connector). ``action`` is the per-column ``PiiAction``.
    """

    label: str
    value: str
    action: PiiAction


@dataclass
class ExternalRecord:
    """One CRM record: a stable non-PII id, typed structured fields, free
    text, and a grouping key for the ACL mapping.

    This is the unit ``crm_sync.py`` runs through ``pii.anonymize`` ->
    ``record_to_tags`` -> ``_insert_document_with_chunks``.
    """

    source_id: str
    crm_group: str
    structured_fields: list[StructuredField] = field(default_factory=list)
    free_text: str = ""


def record_to_tags(record: ExternalRecord, group_map: dict[str, str]) -> list[str]:
    """Map a record's ``crm_group`` to a department tag list, fail-closed.

    ``group_map`` is the operator-authored mapping (CRM grouping value ->
    department name) loaded from the connector spec. The contract:

      * a known, non-empty group present in ``group_map`` -> ``[department]``
      * an empty group, or a group not in ``group_map`` -> ``[CRM_UNMAPPED_TAG]``

    It NEVER returns ``[]`` (public) and NEVER returns an arbitrary tag the
    operator didn't map. The returned tags are still funnelled through
    ``normalise_tags`` + ``assert_known_departments`` by the caller — this
    function only encodes the *fail-closed* policy, not the validation. The
    caller treats a ``CRM_UNMAPPED_TAG`` result as "ingest, but visible to
    admins only".

    The match is case-insensitive on the *group* key (CRM grouping values
    are operator data and may be inconsistently cased); the mapped department
    is returned as authored and validated downstream.
    """
    group = (record.crm_group or "").strip()
    if not group:
        return [CRM_UNMAPPED_TAG]
    # Build a case-insensitive view of the operator map once per call. The
    # map is small (one entry per CRM segment), so this is cheap.
    lowered = {k.strip().lower(): v for k, v in group_map.items()}
    dept = lowered.get(group.lower())
    if not dept:
        return [CRM_UNMAPPED_TAG]
    return [dept]

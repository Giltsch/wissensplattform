"""Pluggable source connectors for the ingest pipeline.

A connector turns "somewhere documents live" into an async stream of
``SourceItem`` envelopes (bytes + provenance) that the shared
``ingest.ingest_item`` pipeline can parse, chunk, embed and version-insert.
The connector seam sits *above* parsing — every connector funnels through
the same ``ingest_item`` so dedup, versioning, the ACL tags decision, and
the per-item caps can never diverge per source.

``SourceItem`` lives here (Phase 3 moved it out of ``ingest.py``); ``ingest``
re-imports it for back-compat so existing call sites and tests that do
``from ingest import SourceItem`` keep working.

v1 ships two connectors: ``local`` (filesystem walk) and ``url`` (an
allow-list of http(s) URLs with an SSRF guard + byte/timeout caps). A
saved-sources table is deferred until a connector needs stored credentials
(e.g. the CRM connector in Phase 4).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass
class SourceItem:
    """One unit of work for the ingest pipeline.

    A thin envelope over "bytes plus where they came from". ``source`` is the
    versioning + display key written to ``documents.source`` (so it must be
    stable across re-syncs of the same logical document); ``content_type`` is
    an optional MIME hint for parse dispatch (empty string falls back to the
    suffix on ``source``); ``suggested_tags`` is a connector-proposed ACL the
    caller may choose to honour — the CLI/admin paths pass ``tags`` explicitly
    and ignore this for now (external sources REQUIRE an explicit tags
    decision; tags are never auto-derived from URL structure).
    """

    source: str
    raw_bytes: bytes
    content_type: str = ""
    suggested_tags: list[str] | None = None


@runtime_checkable
class SourceConnector(Protocol):
    """A source of ``SourceItem`` envelopes.

    Implementations yield items lazily so a large source never has to be
    fully buffered in memory. The driver (CLI or admin endpoint) runs each
    item through ``ingest.ingest_item`` with an explicit ``tags`` and
    ``origin``.
    """

    def iter_items(self) -> AsyncIterator[SourceItem]:
        ...


def connector_factory(spec: dict) -> SourceConnector:
    """Build a connector from a small declarative spec.

    ``spec['type']`` selects the connector; remaining keys are
    connector-specific. Dispatch is explicit (no dynamic import) so a typo'd
    or attacker-influenced ``type`` can only ever reach a known connector.

    Supported:
      * ``{'type': 'local', 'root': <path-or-str>}``
      * ``{'type': 'url', 'urls': [<url>, ...]}``  (optional ``suggested_tags``)
      * ``{'type': 'sql', ...}``  (CRM read-only connector; yields
        ``ExternalRecord``s, driven by ``crm_sync.py`` only — NOT the
        document ingest loop. See ``connectors/sql_connector.py``.)
    """
    kind = spec.get("type")
    if kind == "local":
        # Local import: avoids pulling pathlib/ingest into the import graph of
        # callers that only ever use the url connector.
        from connectors.local import LocalFileConnector

        return LocalFileConnector(spec["root"])
    if kind == "url":
        from connectors.url import UrlConnector

        return UrlConnector(
            spec["urls"], suggested_tags=spec.get("suggested_tags"),
        )
    if kind == "sql":
        # Lazy import keeps psycopg + the CRM types out of the import graph
        # for the always-on backend (which only ever uses local/url).
        from connectors.sql_connector import SqlConnector

        return SqlConnector(spec)
    raise ValueError(f"unknown connector type: {kind!r}")

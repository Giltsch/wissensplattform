"""Read-only external SQL connector for the CRM sync (Phase 4).

Connects to an EXTERNAL database (a read-replica / reporting replica of the
CRM) over an operator-supplied DSN and yields ``ExternalRecord`` envelopes.
Security is the whole point of this module — it is the only place the
platform ever talks to a system it does not own:

  * **Read-only / SELECT-only.** The connector refuses any query that is not
    a single ``SELECT`` (or a leading CTE ``WITH ... SELECT``). It also sets
    the session to read-only where the driver supports it. The DSN MUST point
    at a database role that has only SELECT grants — the SELECT-only check
    here is defense-in-depth, NOT the primary control (a hostile query string
    is operator-authored, but a bug shouldn't be able to mutate the CRM).
  * **No write-back, ever.** There is no UPDATE/INSERT/DELETE path.
  * **Row cap + char cap.** A ``LIMIT`` is appended to the operator query and
    each fetched cell is truncated, so a runaway table can't OOM the sync
    container or balloon the embed step.

The connector is driven by ``crm_sync.py`` only — there is no HTTP trigger.
The query and the column->``PiiAction`` mapping are operator-authored in the
connector spec (see ``crm_sync._load_spec``).

psycopg (Postgres) is imported lazily; MySQL support is a documented TODO —
the same SELECT-only / cap discipline would apply.
"""
from __future__ import annotations

import logging
import re
from typing import AsyncIterator

from connectors.base import ExternalRecord, PiiAction, StructuredField

log = logging.getLogger(__name__)

# A query is accepted only if, after stripping comments and whitespace, it
# begins with SELECT or a WITH ... (CTE) and contains no statement
# terminator that would allow a piggy-backed second statement. This is a
# coarse guard; the authoritative control is a SELECT-only DB role.
_LEADING_SELECT_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
# Statements we must never see in an operator query (belt-and-braces).
_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"merge|call|copy|do|vacuum|analyze)\b",
    re.IGNORECASE,
)


def assert_select_only(query: str) -> None:
    """Raise ValueError unless ``query`` is a single read-only SELECT/CTE.

    Strips SQL comments first so a forbidden keyword can't hide in a
    ``/* ... */`` block, rejects a trailing ``;`` followed by more SQL
    (statement stacking), and rejects any DML/DDL keyword. Operator-authored,
    but a typo here would be the difference between a read and a write against
    the CRM — fail loud and early, before opening a connection."""
    stripped = _COMMENT_RE.sub(" ", query).strip()
    if not stripped:
        raise ValueError("CRM query is empty")
    if not _LEADING_SELECT_RE.match(stripped):
        raise ValueError("CRM query must be a single SELECT (or WITH ... SELECT)")
    # Reject statement stacking: a ';' with anything non-trivial after it.
    head, _, tail = stripped.partition(";")
    if tail.strip():
        raise ValueError("CRM query must be a single statement (no ';' stacking)")
    if _FORBIDDEN_RE.search(head):
        raise ValueError("CRM query contains a forbidden (non-SELECT) keyword")


def _truncate(value: object, max_chars: int) -> str:
    """Stringify and hard-cap one cell value so a giant text column can't
    blow the embed budget. None -> empty string."""
    if value is None:
        return ""
    s = str(value)
    return s[:max_chars]


class SqlConnector:
    """Yield one ``ExternalRecord`` per row of an operator-authored SELECT.

    Spec keys (all operator-authored, validated in ``crm_sync._load_spec``):
      * ``dsn``            — read-only libpq DSN (NEVER logged).
      * ``query``          — a single SELECT; SELECT-only enforced here.
      * ``id_column``      — column holding the stable non-PII source id.
      * ``group_column``   — column holding the crm_group (ACL mapping key).
      * ``free_text_columns`` — list of column names concatenated into
                            ``free_text`` (the NER-backstop target).
      * ``field_actions``  — {column: 'drop'|'pseudonymize'|'keep'} for every
                            other column to emit as a structured field.
      * ``max_records``    — row cap (LIMIT appended to the query).
      * ``max_cell_chars`` — per-cell truncation.
    """

    def __init__(self, spec: dict) -> None:
        self._dsn = spec["dsn"]
        self._query = spec["query"]
        assert_select_only(self._query)
        self._id_column = spec["id_column"]
        self._group_column = spec["group_column"]
        self._free_text_columns = list(spec.get("free_text_columns", []))
        self._field_actions = {
            col: PiiAction(action)
            for col, action in (spec.get("field_actions") or {}).items()
        }
        self._max_records = int(spec["max_records"])
        self._max_cell_chars = int(spec["max_cell_chars"])

    async def iter_items(self) -> AsyncIterator[ExternalRecord]:
        # Lazy import: psycopg is already a backend dep, but keeping the import
        # local means the pure-function tests (assert_select_only) don't need
        # a live driver, and a MySQL-only future deployment wouldn't load it.
        import psycopg
        from psycopg.rows import dict_row

        # Bound the result set with a LIMIT wrapped around the operator query
        # (we don't edit their SQL — we make it a subselect). %s-bound so the
        # cap can't be injection-influenced.
        wrapped = f"SELECT * FROM ({self._query}) AS _crm_src LIMIT %s"

        # read-only session: a SELECT-only role is the real control, but
        # asking the server for a read-only transaction is cheap insurance.
        async with await psycopg.AsyncConnection.connect(
            self._dsn, row_factory=dict_row, autocommit=False,
        ) as conn:
            await conn.set_read_only(True)
            async with conn.cursor() as cur:
                await cur.execute(wrapped, (self._max_records,))
                async for row in cur:
                    yield self._row_to_record(row)
            await conn.rollback()  # never commit a read

    def _row_to_record(self, row: dict) -> ExternalRecord:
        source_id = _truncate(row.get(self._id_column), self._max_cell_chars)
        group = _truncate(row.get(self._group_column), self._max_cell_chars)

        free_parts = [
            _truncate(row.get(col), self._max_cell_chars)
            for col in self._free_text_columns
        ]
        free_text = "\n".join(p for p in free_parts if p)

        fields: list[StructuredField] = []
        for col, action in self._field_actions.items():
            fields.append(
                StructuredField(
                    label=col,
                    value=_truncate(row.get(col), self._max_cell_chars),
                    action=action,
                )
            )
        return ExternalRecord(
            source_id=source_id,
            crm_group=group,
            structured_fields=fields,
            free_text=free_text,
        )

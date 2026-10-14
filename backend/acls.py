"""Tag-based document ACL helpers.

The visibility rule (per migration 011_document_acls.sql):
  * admin                        -> every document
  * documents.tags = '{}'        -> public, every authed user sees it
  * documents.tags && allowed    -> visible (intersection non-empty)
  * otherwise                    -> not visible

This module owns the SQL fragment that encodes that rule and the
canonical tag-string normaliser. Inlining the WHERE clause in three
places (retrieve, list_documents_public, get_document) would be a real
maintenance hazard — one missed filter is a data leak.
"""
from __future__ import annotations

import re
from typing import Iterable

# Tag vocabulary: short ascii slugs. Keeps URLs, logs, and audit rows
# readable, and avoids surprises like collation-sensitive comparisons.
# If you need a localized display name, store it separately — the tag
# itself is the stable identifier.
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def normalise_tag(raw: str) -> str:
    """Normalise an operator-supplied tag string. Returns the canonical form
    or raises ValueError. Trim, lowercase, then validate against the tag
    grammar. Idempotent: normalise(normalise(x)) == normalise(x)."""
    if not isinstance(raw, str):
        raise ValueError("tag must be a string")
    canon = raw.strip().lower()
    if not _TAG_RE.match(canon):
        raise ValueError(
            f"invalid tag {raw!r}: must be 1-32 chars, "
            "[a-z0-9_-], starting with [a-z0-9]"
        )
    return canon


def normalise_tags(raw: Iterable[str]) -> list[str]:
    """Normalise + dedupe a tag list, preserving first-occurrence order.
    A duplicate after normalisation (e.g. ['HR', 'hr']) is silently merged
    rather than raising — operators editing in the UI shouldn't have to
    worry about case."""
    seen: set[str] = set()
    out: list[str] = []
    for r in raw:
        c = normalise_tag(r)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def assert_known_departments(tags: list[str]) -> None:
    """Reject tags that aren't in the departments vocabulary table.

    Migration 012 introduced a controlled list of tag values; any tag set
    on a document or user must exist there. Raises ValueError with the
    list of unknown tags so the API can return a 400 with actionable
    information ('add hr to departments first, then try again').

    Empty list is fine — that's the 'public document' / 'no extra
    access' case and never needs validation.
    """
    if not tags:
        return
    # Local import keeps acls.py free of a hard `db` dependency for the
    # pure-function tests in test_acls.py, which import normalise_*.
    from db import connection
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name FROM departments WHERE name = ANY(%s)", (tags,),
            )
            known = {r["name"] for r in await cur.fetchall()}
    unknown = [t for t in tags if t not in known]
    if unknown:
        raise ValueError(
            f"unknown department(s): {', '.join(unknown)}. "
            "Create them in /api/admin/departments first."
        )


def visibility_clause(
    is_admin: bool, allowed_tags: list[str], doc_alias: str = "d"
) -> tuple[str, list[object]]:
    """Build the SQL WHERE fragment that enforces the document ACL.

    Returns (sql_fragment, params) — the fragment is ready to be appended
    to an existing WHERE clause with `AND`. The fragment uses %s
    placeholders bound by ``params``; the caller must extend its own
    params list in order.

    For admin callers the fragment is the literal ``TRUE``: the planner
    elides it, no extra params bound, no extra index work. For everyone
    else we union the public-doc case (empty tag array) with the
    overlap-with-allowed-tags case.
    """
    if is_admin:
        # Admin bypass — no params consumed. Returning a literal keeps the
        # call site uniform (no `if admin: skip the AND` branching).
        return "TRUE", []
    # cardinality() is the standard Postgres way to test for empty arrays;
    # the equivalent `tags = '{}'` works too but is slightly less obvious
    # at a glance. The && operator is GIN-indexed (see migration 011).
    return (
        f"(cardinality({doc_alias}.tags) = 0 OR {doc_alias}.tags && %s)",
        [allowed_tags],
    )

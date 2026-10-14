"""CRM / external-DB batch sync CLI (Phase 4).

Runs OFFLINE in a SEPARATE one-off container (the 'crm' compose profile) —
NOT in the always-on backend, and NEVER on the chat path. For every record
in the operator-authored read-only SELECT it:

  1. resolves the ACL tags fail-closed (record_to_tags -> normalise_tags ->
     assert_known_departments; unknown/ambiguous -> ['crm_unmapped'], never
     public/[]),
  2. anonymizes the record (pii.anonymize: declared per-field actions first,
     then the Presidio NER backstop on free text),
  3. hashes the SCRUBBED + normalized text (so volatile upstream fields like
     last_contacted_at don't churn document versions),
  4. chunks + embeds (the same Embedder as ingest.py),
  5. version-inserts via _insert_document_with_chunks with
     source='crm:sql:<source_id>' (NEVER any PII in the source string),
     tags=resolved, source_type='crm', source_meta={'connector': 'sql'}.

It writes ONE admin_audit row per run with COUNTS ONLY (records_seen /
ingested / skipped_unmapped / pii_redactions — never values), attributed to
the synthetic system actor (migration 016).

The connector spec (query, column->PiiAction mapping, group->department map)
is operator-authored per deployment and loaded from a JSON file
(``--spec`` / ``CRM_SPEC_FILE``). The DSN, caps, and pseudonym key come from
settings (all SecretStr).

Invariants enforced here and documented in SECURITY.md:
  * scrub-before-embed: raw values never reach hash/embed/store;
  * fail-closed tagging: never public, never an unmapped department;
  * no PII in the source string;
  * read-only / SELECT-only (enforced in the connector);
  * no write-back to the CRM.

FLAG (fast-follow, NOT implemented — see SECURITY.md NOT-done list):
  * the SQL->department mapping is operator-authored per deployment;
  * right-to-erasure (Art. 17): a tombstone pass that retires documents whose
    upstream record has disappeared. Today a deleted CRM row leaves its last
    synced (anonymized) version in place until manually removed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from config import get_settings
from db import apply_migrations, close_pool, connection, init_pool

log = logging.getLogger("crm_sync")

# Stable lookup key for the synthetic system actor seeded by migration 016.
_SYSTEM_ACTOR_EMAIL = "system+crm-sync@localhost.invalid"


def _load_spec(path: Path) -> dict:
    """Load the operator-authored connector spec JSON and merge in the
    secret/cap settings (kept out of the on-disk spec so credentials live
    only in .env). The spec file holds the non-secret, deployment-specific
    shape: query, columns, field_actions, group_map."""
    settings = get_settings()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("CRM spec must be a JSON object")

    # group_map is part of the spec (operator-authored); it is NOT consumed by
    # the connector, only by record_to_tags. Pop it so it doesn't leak into
    # the connector spec.
    group_map = raw.pop("group_map", {})
    if not isinstance(group_map, dict):
        raise ValueError("CRM spec 'group_map' must be an object")

    spec = {
        "type": "sql",
        "dsn": settings.crm_dsn.get_secret_value(),
        "max_records": settings.crm_max_records_per_run,
        "max_cell_chars": settings.crm_max_record_chars,
        **raw,  # query, id_column, group_column, free_text_columns, field_actions
    }
    return {"connector_spec": spec, "group_map": group_map}


async def _resolve_system_actor_id() -> int | None:
    """Look up the synthetic system actor seeded by migration 016. Returns
    None if it's missing (the audit write is then skipped, best-effort)."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM users WHERE email = %s", (_SYSTEM_ACTOR_EMAIL,)
            )
            row = await cur.fetchone()
            return row["id"] if row else None


async def _write_run_audit(counts: dict[str, int]) -> None:
    """Write ONE counts-only admin_audit row for the run. Best-effort: a
    missing system actor or a transient DB hiccup logs but never fails the
    sync (mirrors admin._audit). details are COUNTS ONLY — never any value."""
    actor_id = await _resolve_system_actor_id()
    if actor_id is None:
        log.warning(
            "crm.sync audit skipped: system actor %s not found (run migration 016)",
            _SYSTEM_ACTOR_EMAIL,
        )
        return
    try:
        from psycopg.types.json import Json

        async with connection() as conn:
            async with conn.cursor() as cur:
                # target_type 'crm', target_id NULL (a run, not a single
                # object). ip NULL — there is no request behind a batch job
                # (the _truncate_ip convention only applies to request paths).
                await cur.execute(
                    "INSERT INTO admin_audit("
                    "  actor_id, action, target_type, target_id, details, ip"
                    ") VALUES (%s, %s, %s, %s, %s, %s)",
                    (actor_id, "crm.sync", "crm", None, Json(counts), None),
                )
            await conn.commit()
    except Exception:
        log.exception("crm.sync audit_write_failed")


def _scrubbed_hash(text: str) -> str:
    """sha256 over the scrubbed + normalized text — the dedup/version key.

    Hashing the SCRUBBED text (not raw bytes) means a record whose only
    change upstream was a volatile field the operator declared DROP/
    PSEUDONYMIZE produces the SAME hash, so a re-sync is a no-op instead of a
    new version. It also guarantees no raw identifier influences the key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _run(spec_path: Path) -> int:
    from acls import assert_known_departments, normalise_tags
    from connectors import connector_factory
    from connectors.base import record_to_tags
    from ingest import Embedder, _insert_document_with_chunks, chunk_text, normalize
    from pii import anonymize

    settings = get_settings()
    if not settings.crm_enabled:
        log.error("CRM sync is disabled (set CRM_ENABLED=true). Refusing to run.")
        return 2

    loaded = _load_spec(spec_path)
    connector_spec = loaded["connector_spec"]
    group_map = loaded["group_map"]

    pseudonym_key = settings.pii_pseudonym_key.get_secret_value()
    languages = settings.presidio_language_list
    max_chars = settings.crm_max_record_chars

    embedder = Embedder(settings.embedding_model)

    # Idempotent — safe to run even after the API has migrated.
    await apply_migrations()
    await init_pool()

    counts = {
        "records_seen": 0,
        "ingested": 0,
        "skipped_unmapped": 0,  # records that landed on crm_unmapped
        "skipped_existing": 0,  # dedup no-ops / empty after scrub
        "pii_redactions": 0,
    }
    try:
        connector = connector_factory(connector_spec)
        async for record in connector.iter_items():
            counts["records_seen"] += 1

            # --- ACL tags: fail-closed ---
            raw_tags = record_to_tags(record, group_map)
            tags = normalise_tags(raw_tags)
            # Validate against the controlled vocabulary. crm_unmapped is
            # seeded by migration 016, so this passes; an operator-mapped
            # department that doesn't exist fails loudly (the whole run aborts
            # before more PII is processed).
            await assert_known_departments(tags)
            if tags == ["crm_unmapped"]:
                counts["skipped_unmapped"] += 1
                # NOTE: still ingested — it lands on crm_unmapped (admin-only),
                # which is strictly safer than public. The counter records how
                # many records the operator mapping failed to resolve.

            # --- Anonymize (scrub before anything is hashed/embedded) ---
            result = anonymize(
                record,
                pseudonym_key=pseudonym_key,
                languages=languages,
                max_chars=max_chars,
            )
            counts["pii_redactions"] += result.redactions

            text = normalize(result.text)
            chunks = chunk_text(text)
            if not chunks:
                counts["skipped_existing"] += 1
                log.info("crm record produced no chunks after scrub; skipping")
                continue

            from ingest import sha256_text

            file_hash = _scrubbed_hash(text)
            hashes = [sha256_text(c) for c in chunks]
            embs = await asyncio.to_thread(embedder.encode, chunks)

            # source carries the STABLE NON-PII id only — never any value.
            source = f"crm:sql:{record.source_id}"
            insert = await _insert_document_with_chunks(
                file_hash, source, chunks, hashes, embs,
                tags=tags, origin="local",
                source_type="crm", source_meta={"connector": "sql"},
            )
            if insert is None:
                counts["skipped_existing"] += 1
                continue
            counts["ingested"] += 1

        # TODO (fast-follow, Art. 17 right-to-erasure): a tombstone/
        # reconciliation pass. Collect the set of source_ids seen this run and
        # retire (archive/delete) any source_type='crm' document whose
        # source_id is NOT in that set — i.e. its upstream record disappeared.
        # Out of scope for Phase 4; documented in SECURITY.md "NOT done".
        log.info(
            "crm.sync done seen=%d ingested=%d skipped_unmapped=%d "
            "skipped_existing=%d pii_redactions=%d",
            counts["records_seen"], counts["ingested"], counts["skipped_unmapped"],
            counts["skipped_existing"], counts["pii_redactions"],
        )
        await _write_run_audit(counts)
    finally:
        await close_pool()
    return 0


def main() -> int:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Batch-sync anonymized CRM records into the document store."
    )
    parser.add_argument(
        "--spec", type=Path, default=None,
        help="Path to the operator-authored connector spec JSON "
             "(or set CRM_SPEC_FILE).",
    )
    args = parser.parse_args()
    spec_path = args.spec or (
        Path(os.environ["CRM_SPEC_FILE"]) if os.environ.get("CRM_SPEC_FILE") else None
    )
    if spec_path is None:
        log.error("a connector spec is required (--spec or CRM_SPEC_FILE)")
        return 2
    if not spec_path.exists():
        log.error("spec file does not exist: %s", spec_path)
        return 2

    settings = get_settings()
    interval = settings.crm_sync_interval_s
    if interval <= 0:
        return asyncio.run(_run(spec_path))

    # Optional long-running mode: re-run every interval (mirrors the
    # cleanup.py loop). One pass runs immediately on start.
    async def _loop() -> int:
        while True:
            try:
                await _run(spec_path)
            except Exception:
                log.exception("crm.sync pass failed; retrying next interval")
            await asyncio.sleep(interval)

    try:
        return asyncio.run(_loop())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

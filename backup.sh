#!/usr/bin/env bash
# Daily Postgres backup. Designed to run from cron on the host.
#
# - Dumps via `docker compose exec`, no need to publish Postgres' port.
# - Custom format (`pg_dump --format=custom`) is the smallest and supports
#   parallel restore. Restore with `pg_restore`, not `psql`.
# - Retention: dumps older than $RETENTION_DAYS are deleted (default 14).
# - Optional encryption with `age`: set BACKUP_AGE_RECIPIENT to an age
#   public key. Requires the `age` binary on PATH.
#
# Restore a single dump:
#   docker compose exec -T postgres pg_restore --clean --if-exists \
#     -U "$POSTGRES_USER" -d "$POSTGRES_DB" < ragdb-YYYYMMDD....dump
#
# Cron example (daily at 03:14 UTC):
#   14 3 * * * /opt/rag/backup.sh >> /var/log/rag-backup.log 2>&1

set -euo pipefail

cd "$(dirname "$0")"

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
DUMP_FILE="$BACKUP_DIR/ragdb-$TS.dump"

mkdir -p "$BACKUP_DIR"

# Encryption is now fail-closed. Backups contain bcrypt password hashes,
# chat content (PII), TOTP secrets, IP logs — leaking even one snapshot
# is a full plattform compromise. Set BACKUP_AGE_RECIPIENT to an age
# public key; the matching secret key stays off this VPS (audit
# C6-HIGH-7, 2026-05-12).
#
# Local-test escape hatch: ALLOW_UNENCRYPTED_BACKUP=1 — explicit so you
# can't unset the recipient by accident in production.
if [[ -z "${BACKUP_AGE_RECIPIENT:-}" && "${ALLOW_UNENCRYPTED_BACKUP:-}" != "1" ]]; then
  echo "FAIL: refusing to write an unencrypted backup." >&2
  echo "Set BACKUP_AGE_RECIPIENT=<age1...> (recommended) — or set" >&2
  echo "ALLOW_UNENCRYPTED_BACKUP=1 explicitly if this is a local test." >&2
  exit 1
fi
if [[ -n "${BACKUP_AGE_RECIPIENT:-}" ]] && ! command -v age >/dev/null 2>&1; then
  echo "FAIL: BACKUP_AGE_RECIPIENT set but 'age' not on PATH" >&2
  exit 1
fi

# Run pg_dump inside the postgres container — POSTGRES_USER/PASSWORD/DB are
# already set there by docker-compose, so we don't need to source .env on
# the host (which gets messy with quoted values).
docker compose exec -T postgres bash -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner' \
  > "$DUMP_FILE"

if [[ -n "${BACKUP_AGE_RECIPIENT:-}" ]]; then
  age -r "$BACKUP_AGE_RECIPIENT" -o "$DUMP_FILE.age" "$DUMP_FILE"
  rm "$DUMP_FILE"
  DUMP_FILE="$DUMP_FILE.age"
fi

# Retention. -mtime is days in 24h units; +N matches strictly older than N.
find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'ragdb-*.dump' -o -name 'ragdb-*.dump.age' \) \
  -mtime "+$RETENTION_DAYS" -delete

SIZE=$(du -h "$DUMP_FILE" | cut -f1)
echo "ok: $DUMP_FILE ($SIZE)"

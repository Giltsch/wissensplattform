#!/usr/bin/env bash
# End-to-end smoke test. Spins up the stack, ingests a tiny doc, creates a
# user, logs in, and runs one chat query. Exits 0 on success.
#
# Requires: docker, docker compose, .env present with OPENROUTER_API_KEY.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "FAIL: .env missing — copy .env.example to .env and fill in" >&2
  exit 2
fi

EMAIL="${SMOKE_EMAIL:-smoke@example.com}"
# A randomly-generated per-run password — avoids the previous
# hardcoded fixture that would leave a guessable account live after a
# Production smoke-run (audit C4-LOW-15, 2026-05-12).
PASSWORD="${SMOKE_PASSWORD:-$(openssl rand -hex 24)}"
COOKIES="$(mktemp)"

cleanup() {
  rm -f "$COOKIES"
  # Best-effort: deactivate the smoke user so a Production smoke-run
  # doesn't leave a viable account behind. Uses the postgres
  # container's own env vars (set by docker-compose from .env) so
  # the host script doesn't need them. Ignores errors if the stack
  # is already torn down.
  docker compose exec -T postgres bash -c \
    "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \"UPDATE users SET is_active = FALSE WHERE email = '$EMAIL';\"" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

step "1/6 docker compose up"
docker compose up -d --build

step "2/6 wait for backend healthy"
for _ in $(seq 1 60); do
  s="$(docker compose ps --format json | python3 -c "import sys,json; [print(json.loads(l).get('Health','')) for l in sys.stdin if 'backend' in l]" || true)"
  [[ "$s" == "healthy" ]] && break
  sleep 2
done
docker compose ps

step "3/6 sample doc"
mkdir -p docs
cat > docs/_smoke.md <<'EOF'
# Smoke Doc

The internal coffee policy: every employee is entitled to two free espressos
per day from the office kitchen. Expense reimbursement uses the WizExp portal
and reimbursements are paid the following Friday.
EOF

step "4/6 ingest"
docker compose exec -T backend python ingest.py /app/docs

step "5/6 create user + login"
docker compose exec -T backend python -m auth add-user "$EMAIL" --password "$PASSWORD"

# Use the frontend nginx as the entry point — same path as a browser hits.
BASE="${SMOKE_BASE:-http://localhost:8080}"
http_code=$(curl -sS -o /dev/null -w "%{http_code}" -c "$COOKIES" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" \
  "$BASE/api/login")
[[ "$http_code" == "200" ]] || { echo "FAIL: login returned $http_code"; exit 1; }

step "6/6 chat (SSE)"
curl -sS -N -b "$COOKIES" \
  -H "Content-Type: application/json" \
  -d '{"message":"How many free espressos per day?"}' \
  "$BASE/api/chat" | tee /tmp/smoke_chat.out

# Require at least one token event and the done event.
grep -q '^event: token'  /tmp/smoke_chat.out || { echo "FAIL: no tokens streamed"; exit 1; }
grep -q '^event: done'   /tmp/smoke_chat.out || { echo "FAIL: stream did not complete"; exit 1; }
grep -q '_smoke.md'       /tmp/smoke_chat.out || echo "WARN: smoke doc not cited (LLM may have answered without citation)"

echo
echo "OK: smoke test passed"

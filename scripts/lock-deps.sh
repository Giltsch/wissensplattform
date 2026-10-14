#!/usr/bin/env bash
# Generate a hashed requirements lockfile so `pip install --require-hashes`
# can detect tampered packages at install time (audit C4-HIGH-6, 2026-05-12).
#
# Run on the developer machine, NOT in CI. Commits the regenerated
# requirements.txt with --hash=sha256:... entries per package.
#
# Requires pip-tools (https://github.com/jazzband/pip-tools). Install
# once: `pip install pip-tools==7.4.1`.
#
# Workflow:
#   1. Edit backend/requirements.in (unpinned spec — created on first run
#      from the existing requirements.txt).
#   2. `./scripts/lock-deps.sh`
#   3. Review the diff, commit requirements.txt.
#   4. Rebuild containers — Dockerfile will use --require-hashes once
#      hashes are present (pip auto-detects).
#
# Why not just use poetry / uv? — pip-tools stays closest to plain pip and
# the existing requirements.txt format; no new tooling for operators.

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v pip-compile >/dev/null 2>&1; then
    echo "pip-compile not found. Install with: pip install pip-tools==7.4.1" >&2
    exit 1
fi

# On first run, seed requirements.in from the existing requirements.txt
# by stripping any existing --hash= lines.
if [[ ! -f backend/requirements.in ]]; then
    echo "Seeding backend/requirements.in from requirements.txt"
    grep -v -- '--hash=' backend/requirements.txt | grep -v '^\s*\\' > backend/requirements.in
fi
if [[ ! -f backend/requirements-test.in ]]; then
    echo "Seeding backend/requirements-test.in from requirements-test.txt"
    grep -v -- '--hash=' backend/requirements-test.txt | grep -v '^\s*\\' > backend/requirements-test.in
fi

# Generate lockfiles. --allow-unsafe pins setuptools/pip itself; --strip-extras
# avoids over-locking marker-extras that pip can't always re-evaluate.
pip-compile --generate-hashes --allow-unsafe --strip-extras \
            --output-file backend/requirements.txt \
            backend/requirements.in

pip-compile --generate-hashes --allow-unsafe --strip-extras \
            --output-file backend/requirements-test.txt \
            backend/requirements-test.in

echo
echo "Regenerated backend/requirements.txt and backend/requirements-test.txt with hashes."
echo "Review the diff:"
echo "  git diff backend/requirements.txt backend/requirements-test.txt"
echo "Then rebuild containers:"
echo "  docker compose build --no-cache backend"

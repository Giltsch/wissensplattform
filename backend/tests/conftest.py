"""Shared test fixtures.

The unit suite must import ``config`` / ``auth`` / ``prompts`` without a
real .env present (CI runs in a clean environment). We populate the
required Settings fields with throwaway values *before* any test module
imports those modules — the ``lru_cache`` on ``get_settings()`` then
freezes those values for the whole session.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the backend package importable: tests live in backend/tests/, but
# they import top-level modules (`config`, `auth`, `prompts`, ...).
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Required Settings fields. Values don't matter as long as they parse —
# tests never hit Postgres or OpenRouter.
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault(
    "SESSION_SECRET",
    "test-secret-not-for-production-minimum-32-chars",
)
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-not-real")

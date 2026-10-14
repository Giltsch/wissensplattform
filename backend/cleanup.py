"""Periodic cleanup of expired auth-state rows + DSGVO-driven retention.

Originally (audit L-6, 2026-05-10) this only reaped ``sessions`` and
``login_challenges`` so they didn't grow forever. Extended (audit
C6-HIGH-5, 2026-05-12) to enforce the retention windows the
Datenschutzerklärung promises for ``login_attempts``, ``chat_usage``,
``admin_audit``, and optionally ``messages``. Without these sweeps,
the data lives forever — a Speicherbegrenzungs-Verstoß under
DSGVO Art. 5 Abs. 1 lit. e.

The cleanup is a background asyncio task started by main.lifespan(). It
runs once on startup (so a long-stopped deployment catches up immediately)
and then every CLEANUP_INTERVAL_S thereafter. Failures are logged but
never crash the loop — a transient DB hiccup must not take down the task
permanently.
"""
from __future__ import annotations

import asyncio
import logging

from config import get_settings
from db import connection

log = logging.getLogger(__name__)

# Run every 6 hours. Cleanup is cheap (small indexed deletes) so we could
# go more frequent, but expired rows are harmless until they pile up — no
# reason to add load.
CLEANUP_INTERVAL_S = 6 * 3600

# Grace periods on top of the row's own expires_at: keep expired rows
# around briefly so a clock-skewed client doesn't see "session vanished
# mid-request". 7 days for sessions matches typical password-grace
# expectations; 1 day for challenges is generous (real TTL is 5 min).
SESSION_GRACE_DAYS = 7
CHALLENGE_GRACE_DAYS = 1


async def _run_once() -> None:
    """One cleanup pass. Defensive about errors — never raises.

    All intervals are passed via parameters (cast to `interval` server-
    side) — never string-interpolated, even though SESSION_GRACE_DAYS
    et al. are module constants today. Avoiding f-string SQL keeps the
    next operator who decides to load retention windows from `.env`
    from accidentally opening an injection sink (audit C3-LOW-1,
    2026-05-12).
    """
    settings = get_settings()
    try:
        async with connection() as conn:
            async with conn.cursor() as cur:
                # Auth-state hygiene (always on).
                await cur.execute(
                    "DELETE FROM sessions "
                    "WHERE expires_at < now() - (%s || ' days')::interval",
                    (str(SESSION_GRACE_DAYS),),
                )
                sessions_deleted = cur.rowcount
                await cur.execute(
                    "DELETE FROM login_challenges "
                    "WHERE expires_at < now() - (%s || ' days')::interval",
                    (str(CHALLENGE_GRACE_DAYS),),
                )
                challenges_deleted = cur.rowcount

                # DSGVO retention sweeps. A value of 0 disables the
                # sweep for that table.
                login_attempts_deleted = 0
                if settings.retention_login_attempts_days > 0:
                    await cur.execute(
                        "DELETE FROM login_attempts "
                        "WHERE occurred_at < now() - (%s || ' days')::interval",
                        (str(settings.retention_login_attempts_days),),
                    )
                    login_attempts_deleted = cur.rowcount

                chat_usage_deleted = 0
                if settings.retention_chat_usage_days > 0:
                    await cur.execute(
                        "DELETE FROM chat_usage "
                        "WHERE day < CURRENT_DATE - (%s || ' days')::interval",
                        (str(settings.retention_chat_usage_days),),
                    )
                    chat_usage_deleted = cur.rowcount

                admin_audit_deleted = 0
                if settings.retention_admin_audit_days > 0:
                    await cur.execute(
                        "DELETE FROM admin_audit "
                        "WHERE occurred_at < now() - (%s || ' days')::interval",
                        (str(settings.retention_admin_audit_days),),
                    )
                    admin_audit_deleted = cur.rowcount

                # Messages CASCADE through conversations.id; deleting
                # messages directly leaves orphan conversations the
                # user might still want listed. Strategy: when a
                # conversation has had no activity for the window,
                # delete the conversation — ON DELETE CASCADE wipes
                # the messages.
                messages_deleted = 0
                if settings.retention_messages_days > 0:
                    await cur.execute(
                        "DELETE FROM conversations "
                        "WHERE updated_at < now() - (%s || ' days')::interval",
                        (str(settings.retention_messages_days),),
                    )
                    messages_deleted = cur.rowcount

            await conn.commit()
        log.info(
            "cleanup ok sessions=%d challenges=%d login_attempts=%d "
            "chat_usage=%d admin_audit=%d stale_conversations=%d",
            sessions_deleted, challenges_deleted, login_attempts_deleted,
            chat_usage_deleted, admin_audit_deleted, messages_deleted,
        )
    except Exception:
        # The whole point of this task is "best-effort hygiene". Log loud
        # and try again at the next interval.
        log.exception("cleanup failed")


async def run_periodic() -> None:
    """Run forever, sleeping CLEANUP_INTERVAL_S between passes.

    Started by lifespan() as ``asyncio.create_task(run_periodic())``. The
    task is cancelled on shutdown — the CancelledError is swallowed so
    the lifespan exit isn't noisy with a traceback for the expected case.
    """
    log.info("cleanup task started (interval %ds)", CLEANUP_INTERVAL_S)
    try:
        while True:
            await _run_once()
            await asyncio.sleep(CLEANUP_INTERVAL_S)
    except asyncio.CancelledError:
        log.info("cleanup task cancelled")
        raise

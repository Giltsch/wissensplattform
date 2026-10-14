"""Postgres connection pool and migration runner.

We use psycopg 3's async pool because FastAPI handlers are async, and
register the pgvector adapter so we can pass numpy arrays directly to queries.

Startup ordering matters:
1. apply_migrations() opens a *non-pooled* connection and runs the SQL
   files. The first migration creates the `vector` extension.
2. init_pool() opens the pool. The pool's configure callback registers the
   pgvector type adapter on each connection — this requires the extension
   to already exist, hence the explicit ordering.

Migrations are plain `.sql` files in `migrations/`, applied in lexical order,
tracked in `schema_migrations`. Each file should be self-idempotent
(`CREATE TABLE IF NOT EXISTS` etc.) so that re-running on an already-migrated
database is a no-op.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pgvector.psycopg import register_vector_async

from config import get_settings

log = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def _configure_connection(conn: AsyncConnection) -> None:
    conn.row_factory = dict_row
    # The pgvector adapter looks up the `vector` type in pg_type. Migrations
    # must have run before the pool opens.
    await register_vector_async(conn)


async def _wait_for_postgres(dsn: str, attempts: int = 30, delay: float = 1.0) -> None:
    """Poll until Postgres accepts connections.

    Compose's healthcheck on `postgres` already gates startup, but on rare
    races (image first-run init) the server can briefly accept TCP without
    yet allowing logins. Cheap to retry; protects the lifespan from a
    spurious crash loop.
    """
    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            async with await psycopg.AsyncConnection.connect(dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            return
        except Exception as e:
            last_err = e
            await asyncio.sleep(delay)
    raise RuntimeError(f"postgres unreachable after {attempts} attempts: {last_err}")


async def apply_migrations() -> None:
    """Apply any pending migrations on a non-pooled connection.

    Must run before init_pool() so the `vector` extension exists when the
    pool's configure callback registers its type adapter.

    Concurrency: when (eventually) more than one backend container is
    started against the same DB, both call `apply_migrations()` at
    boot. Without a lock they'd race on the same `INSERT INTO
    schema_migrations` and one would likely fail with a unique-violation
    or worse, both apply a destructive migration twice. A session-level
    `pg_advisory_lock` serialises them — first one wins, the others
    wait until release and then find every migration already applied
    (audit C3-LOW-10, 2026-05-12).

    The lock key (`0x77697373` = ASCII 'wiss') is just a process-wide
    constant; any two backends sharing the DB will pick the same key.
    """
    settings = get_settings()
    await _wait_for_postgres(settings.dsn)

    if not MIGRATIONS_DIR.is_dir():
        log.warning("migrations directory missing: %s", MIGRATIONS_DIR)
        return
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        return

    _MIGRATIONS_LOCK_KEY = 0x77697373  # 'w'ISS — arbitrary but stable

    async with await psycopg.AsyncConnection.connect(settings.dsn) as conn:
        conn.row_factory = dict_row
        async with conn.cursor() as cur:
            # Take a session-level lock — blocks any other backend that
            # is also booting against this DB until we COMMIT/disconnect.
            await cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATIONS_LOCK_KEY,))
            # Make the tracking table available even before the first
            # migration runs (the init script also creates it idempotently).
            await cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            await cur.execute("SELECT version FROM schema_migrations")
            applied = {row["version"] for row in await cur.fetchall()}
        await conn.commit()

        try:
            for path in files:
                version = path.stem
                if version in applied:
                    continue
                log.info("applying migration %s", version)
                sql = path.read_text(encoding="utf-8")
                async with conn.cursor() as cur:
                    await cur.execute(sql)  # type: ignore[arg-type]
                    await cur.execute(
                        "INSERT INTO schema_migrations(version) VALUES (%s) "
                        "ON CONFLICT (version) DO NOTHING",
                        (version,),
                    )
                await conn.commit()
        finally:
            # Released automatically when the session closes, but we
            # release explicitly for clarity.
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_unlock(%s)", (_MIGRATIONS_LOCK_KEY,),
                )
            await conn.commit()


async def init_pool() -> AsyncConnectionPool:
    """Open the global pool. Call after apply_migrations()."""
    global _pool
    if _pool is not None:
        return _pool
    settings = get_settings()
    # max_size=20 leaves headroom for ~20 concurrent users each briefly
    # holding 1–2 connections during a chat turn (retrieve + persist).
    # Each connection sits at ~10 MB RSS server-side; 20 idle connections
    # is still <250 MB on the Postgres container.
    _pool = AsyncConnectionPool(
        conninfo=settings.dsn,
        min_size=2,
        max_size=20,
        open=False,
        configure=_configure_connection,
        max_idle=300,
    )
    await _pool.open(wait=True, timeout=30)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised; call init_pool() first")
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[AsyncConnection]:
    """Borrow a connection from the pool."""
    async with get_pool().connection() as conn:
        yield conn

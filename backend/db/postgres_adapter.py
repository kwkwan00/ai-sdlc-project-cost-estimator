"""Async SQLAlchemy engine + session lifecycle for Postgres.

Module-level state mirrors the Neo4j adapter: an engine is created lazily on first
use, cached, and disposed on backend shutdown. Every public function tolerates the
"Postgres disabled" case (no DSN, or connect failure) by returning None / no-op so
the backend keeps serving requests when Postgres is down — only history persistence
and twin calibration are degraded.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_init_attempted = False


def _build_engine() -> AsyncEngine | None:
    settings = get_settings()
    dsn = settings.resolved_postgres_dsn
    if not dsn:
        logger.warning(
            "Postgres disabled (no POSTGRES_DSN / POSTGRES_PASSWORD). "
            "History persistence + twin calibration will no-op."
        )
        return None
    try:
        engine = create_async_engine(
            dsn,
            pool_size=settings.postgres_pool_size,
            max_overflow=settings.postgres_max_overflow,
            pool_pre_ping=True,
            future=True,
        )
        logger.info("Postgres engine created (pool_size=%s)", settings.postgres_pool_size)
        return engine
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres engine creation failed (%s); persistence disabled", exc)
        return None


def get_engine() -> AsyncEngine | None:
    """Return the cached async engine, building it on first call.

    Returns None when Postgres is intentionally or accidentally disabled — callers
    must treat that as "skip persistence" rather than raise.
    """
    global _engine, _sessionmaker, _init_attempted
    if _engine is not None:
        return _engine
    if _init_attempted:
        return None
    _init_attempted = True
    _engine = _build_engine()
    if _engine is not None:
        _sessionmaker = async_sessionmaker(
            _engine, expire_on_commit=False, class_=AsyncSession
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession] | None:
    if _sessionmaker is None:
        get_engine()  # populates _sessionmaker as a side effect
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession | None]:
    """Yield a session that commits on clean exit, rolls back on exception.

    Yields None when Postgres is disabled — call sites use `if session is None: return`
    to short-circuit gracefully.
    """
    maker = get_sessionmaker()
    if maker is None:
        logger.debug("Postgres disabled; session_scope yielding None (persistence skipped)")
        yield None
        return
    session = maker()
    try:
        yield session
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    """Close the engine + pool. Safe to call multiple times."""
    global _engine, _sessionmaker, _init_attempted
    if _engine is not None:
        await _engine.dispose()
        logger.info("Postgres engine disposed")
    _engine = None
    _sessionmaker = None
    _init_attempted = False


def _reset_for_tests() -> None:
    """Test-only hook: clear cached engine so a new DSN can be installed.

    Production code never calls this; tests use it after swapping settings to point
    at an aiosqlite in-memory DB.
    """
    global _engine, _sessionmaker, _init_attempted
    _engine = None
    _sessionmaker = None
    _init_attempted = False


async def ping() -> bool:
    """Cheap connectivity check used by startup migrate-on-start logic."""
    engine = get_engine()
    if engine is None:
        return False
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres ping failed: %s", exc)
        return False

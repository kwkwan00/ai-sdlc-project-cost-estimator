"""Programmatic Alembic upgrade — invoked from the FastAPI lifespan.

Equivalent to `uv run alembic upgrade head`, but importable so the backend can
self-migrate when POSTGRES_MIGRATE_ON_START=true. Falls back to a logged warning
on failure rather than crashing the process — operators can run `alembic upgrade
head` manually to recover.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config

from alembic import command
from config import get_settings

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _BACKEND_DIR / "alembic.ini"


def upgrade_to_head() -> bool:
    """Run Alembic upgrade head. Returns True on success, False on any failure.

    Skips silently when Postgres is disabled or migrations are turned off via
    POSTGRES_MIGRATE_ON_START=false.
    """
    settings = get_settings()
    if not settings.postgres_enabled:
        logger.info("Postgres disabled — skipping Alembic upgrade")
        return False
    if not settings.postgres_migrate_on_start:
        logger.info("POSTGRES_MIGRATE_ON_START=false — skipping Alembic upgrade")
        return False

    if not _ALEMBIC_INI.exists():
        logger.warning("alembic.ini not found at %s — skipping migrations", _ALEMBIC_INI)
        return False

    try:
        cfg = Config(str(_ALEMBIC_INI))
        # Run env.py WITHOUT applying alembic.ini's logging config: in-process here it would
        # reconfigure the root logger to WARN + a stderr handler, clobbering the app's INFO/stdout
        # logging for the rest of the process (the CLI path leaves this True). See alembic/env.py.
        cfg.attributes["configure_logger"] = False
        cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", settings.resolved_postgres_dsn)
        logger.info("Running Alembic upgrade head")
        command.upgrade(cfg, "head")
        logger.info("Alembic upgrade head completed")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Alembic upgrade failed (%s); continuing without migration", exc)
        return False

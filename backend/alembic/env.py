"""Alembic env — async, configured against the app's resolved Postgres DSN.

Importing `config.get_settings()` ensures we honor POSTGRES_DSN / discrete vars,
so `alembic upgrade head` works both via the Makefile (host shell) and from inside
the dockerized backend container.
"""

from __future__ import annotations

import asyncio

# Make backend/ importable when alembic is invoked from `backend/` as CWD.
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_settings  # noqa: E402
from db.orm_models import Base  # noqa: E402  — autogenerate target

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Always prefer the app's resolved DSN over alembic.ini's placeholder.
_resolved_dsn = get_settings().resolved_postgres_dsn
if _resolved_dsn:
    config.set_main_option("sqlalchemy.url", _resolved_dsn)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection (`alembic upgrade head --sql`)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations using the app's async engine config."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        future=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

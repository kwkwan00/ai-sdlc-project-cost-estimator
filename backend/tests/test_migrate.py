"""Coverage for the programmatic Alembic upgrade helper.

Real migration execution is exercised in test_postgres_layer via
Base.metadata.create_all (same schema). These tests only assert the short-circuit
branches that protect the backend from crashing during startup.
"""

from __future__ import annotations

import pytest

from db import migrate as migrate_mod


def test_upgrade_to_head_skips_when_postgres_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DSN, no password → returns False without touching alembic."""

    class _Disabled:
        postgres_enabled = False
        postgres_migrate_on_start = True
        resolved_postgres_dsn = ""

    monkeypatch.setattr(migrate_mod, "get_settings", lambda: _Disabled())
    assert migrate_mod.upgrade_to_head() is False


def test_upgrade_to_head_skips_when_migrate_on_start_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OffSwitch:
        postgres_enabled = True
        postgres_migrate_on_start = False
        resolved_postgres_dsn = "postgresql+asyncpg://x:y@h:1/z"

    monkeypatch.setattr(migrate_mod, "get_settings", lambda: _OffSwitch())
    assert migrate_mod.upgrade_to_head() is False


def test_upgrade_to_head_returns_false_when_alembic_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken alembic invocation must NOT crash the lifespan."""

    class _Enabled:
        postgres_enabled = True
        postgres_migrate_on_start = True
        resolved_postgres_dsn = "postgresql+asyncpg://x:y@h:1/z"

    monkeypatch.setattr(migrate_mod, "get_settings", lambda: _Enabled())

    def boom(_cfg, _rev):
        raise RuntimeError("alembic exploded")

    monkeypatch.setattr(migrate_mod.command, "upgrade", boom)
    assert migrate_mod.upgrade_to_head() is False


def test_upgrade_to_head_disables_alembic_logging_reconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-process migrations must NOT let env.py apply alembic.ini's logging config — doing so
    reconfigures the root logger to WARN + a stderr handler and silently suppresses the app's INFO
    logs (the '✓ Backend ready' line, request logs) for the rest of the process. The fix sets
    cfg.attributes['configure_logger']=False, which env.py honors. Regression for that."""

    class _Enabled:
        postgres_enabled = True
        postgres_migrate_on_start = True
        resolved_postgres_dsn = "postgresql+asyncpg://x:y@h:1/z"

    monkeypatch.setattr(migrate_mod, "get_settings", lambda: _Enabled())

    captured: dict = {}

    def _capture(cfg, _rev):
        captured["configure_logger"] = cfg.attributes.get("configure_logger")

    monkeypatch.setattr(migrate_mod.command, "upgrade", _capture)
    assert migrate_mod.upgrade_to_head() is True
    # env.py skips fileConfig() when this is False (the CLI path defaults it True).
    assert captured["configure_logger"] is False

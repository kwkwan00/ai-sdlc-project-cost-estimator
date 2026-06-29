from __future__ import annotations

from config import Settings


def test_cors_origin_list_splits_and_strips() -> None:
    s = Settings(BACKEND_CORS_ORIGINS="http://localhost:3000,  http://127.0.0.1:3000 ,")
    assert s.cors_origin_list == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_cors_origin_list_single_origin() -> None:
    s = Settings(BACKEND_CORS_ORIGINS="http://localhost:3000")
    assert s.cors_origin_list == ["http://localhost:3000"]


def test_postgres_disabled_when_no_password_or_dsn() -> None:
    s = Settings(POSTGRES_PASSWORD="", POSTGRES_DSN="")
    assert s.resolved_postgres_dsn == ""
    assert s.postgres_enabled is False


def test_postgres_dsn_assembled_from_discrete_vars() -> None:
    s = Settings(
        POSTGRES_USER="alice",
        POSTGRES_PASSWORD="secret",
        POSTGRES_DB="estimator_test",
        POSTGRES_HOST="db",
        POSTGRES_PORT=15432,
        POSTGRES_DSN="",
    )
    assert s.resolved_postgres_dsn == (
        "postgresql+asyncpg://alice:secret@db:15432/estimator_test"
    )
    assert s.postgres_enabled is True


def test_postgres_dsn_explicit_overrides_discrete_vars() -> None:
    explicit = "postgresql+asyncpg://override:over@host:5432/db"
    s = Settings(
        POSTGRES_USER="ignored",
        POSTGRES_PASSWORD="ignored",
        POSTGRES_DSN=explicit,
    )
    assert s.resolved_postgres_dsn == explicit


def test_wbs_model_accepts_legacy_anthropic_model_wbs_alias() -> None:
    # The setting was renamed ANTHROPIC_MODEL_WBS -> WBS_MODEL; the legacy env var must still bind so
    # an existing deployment's override isn't silently dropped, with WBS_MODEL winning when both are
    # set. (model_validate so we exercise the alias mapping directly, free of any ambient .env.)
    assert Settings.model_validate({"ANTHROPIC_MODEL_WBS": "claude-sonnet-4-6"}).wbs_model == (
        "claude-sonnet-4-6"
    )
    assert Settings.model_validate(
        {"WBS_MODEL": "gpt-5.5", "ANTHROPIC_MODEL_WBS": "claude-sonnet-4-6"}
    ).wbs_model == "gpt-5.5"

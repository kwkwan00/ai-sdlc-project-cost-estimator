from __future__ import annotations

from config import Settings


def test_langfuse_disabled_when_keys_missing() -> None:
    s = Settings(LANGFUSE_PUBLIC_KEY="", LANGFUSE_SECRET_KEY="")
    assert s.langfuse_enabled is False


def test_langfuse_disabled_when_only_one_key_set() -> None:
    s = Settings(LANGFUSE_PUBLIC_KEY="pk-lf-x", LANGFUSE_SECRET_KEY="")
    assert s.langfuse_enabled is False


def test_langfuse_enabled_when_both_keys_set() -> None:
    s = Settings(LANGFUSE_PUBLIC_KEY="pk-lf-x", LANGFUSE_SECRET_KEY="sk-lf-y")
    assert s.langfuse_enabled is True


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

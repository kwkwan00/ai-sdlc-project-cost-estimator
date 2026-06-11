from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-opus-4-5-20250929", alias="ANTHROPIC_MODEL")
    # Per-agent model overrides for the two lightweight pre-submission agents.
    # These are independent of ANTHROPIC_MODEL (which the six estimation twins
    # use) so the prefill/roster helpers don't inherit the heavyweight Opus model.
    anthropic_model_prefill: str = Field(
        default="claude-haiku-4-5", alias="ANTHROPIC_MODEL_PREFILL"
    )
    anthropic_model_roster: str = Field(
        default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL_ROSTER"
    )

    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(default="", alias="NEO4J_PASSWORD")
    neo4j_database: str = Field(default="neo4j", alias="NEO4J_DATABASE")

    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    qdrant_api_key: str = Field(default="", alias="QDRANT_API_KEY")

    postgres_user: str = Field(default="estimator", alias="POSTGRES_USER")
    postgres_password: str = Field(default="", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="estimator", alias="POSTGRES_DB")
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    # Full DSN takes precedence when set. Use asyncpg driver so SQLAlchemy 2.0
    # async works out of the box.
    postgres_dsn: str = Field(default="", alias="POSTGRES_DSN")
    postgres_migrate_on_start: bool = Field(default=True, alias="POSTGRES_MIGRATE_ON_START")
    postgres_pool_size: int = Field(default=5, alias="POSTGRES_POOL_SIZE")
    postgres_max_overflow: int = Field(default=5, alias="POSTGRES_MAX_OVERFLOW")

    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(default="https://cloud.langfuse.com", alias="LANGFUSE_HOST")

    backend_host: str = Field(default="0.0.0.0", alias="BACKEND_HOST")
    backend_port: int = Field(default=8000, alias="BACKEND_PORT")
    cors_origins: str = Field(default="http://localhost:3000", alias="BACKEND_CORS_ORIGINS")

    # Root log level for the backend (DEBUG | INFO | WARNING | ERROR). Applied by
    # observability.logging_config.configure_logging() at startup.
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def resolved_postgres_dsn(self) -> str:
        """Prefer an explicit POSTGRES_DSN; otherwise assemble from discrete vars.

        Returns "" when no password is set — callers must treat that as "disabled"
        so the backend keeps running without Postgres (mirrors Neo4j behavior).
        """
        if self.postgres_dsn:
            return self.postgres_dsn
        if not self.postgres_password:
            return ""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_enabled(self) -> bool:
        return bool(self.resolved_postgres_dsn)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

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
    anthropic_model: str = Field(default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL")
    # Per-agent model overrides. These are independent of ANTHROPIC_MODEL (which the
    # six estimation twins use, defaulting to Sonnet) so each helper can pin its own
    # tier: the lightweight pre-submission/merge agents stay on Haiku, while the
    # roster/tooling agents that need broader knowledge match the twins on Sonnet.
    anthropic_model_prefill: str = Field(
        default="claude-haiku-4-5", alias="ANTHROPIC_MODEL_PREFILL"
    )
    anthropic_model_roster: str = Field(
        default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL_ROSTER"
    )
    # Lightweight consolidation pass that clusters near-duplicate clarifying
    # questions in merge_pass1. Cheap/fast; degrades to deterministic topic-dedup
    # when unset or unreachable.
    anthropic_model_merge: str = Field(
        default="claude-haiku-4-5", alias="ANTHROPIC_MODEL_MERGE"
    )
    # Classifies the freeform Stage 3 AI-tooling description into per-phase levels.
    # Benefits from broad tool knowledge, so defaults to Sonnet rather than Haiku.
    anthropic_model_tooling: str = Field(
        default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL_TOOLING"
    )
    # LLM-as-judge for the evals harness (backend/evals/) defaults to OpenAI GPT-5.5 —
    # the judge is intentionally a DIFFERENT provider from the Anthropic twins it
    # grades (less same-model self-preference bias). `OPENAI_API_KEY` authenticates it;
    # `OPENAI_MODEL_EVAL` (or `python -m evals.run --judge-model`) overrides the model.
    # Passing an Anthropic `--judge-model claude-*` still works — the judge falls back
    # to call_structured, and `anthropic_model_eval` is that fallback's default.
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model_eval: str = Field(default="gpt-5.5", alias="OPENAI_MODEL_EVAL")
    anthropic_model_eval: str = Field(
        default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL_EVAL"
    )
    # Self-hosted docs-mcp-server the tooling classifier consults (as an MCP client
    # over streamable HTTP) to research tools it doesn't recognize. Co-located in
    # docker-compose; empty url disables research → unknown tools stay 'none'.
    docs_mcp_url: str = Field(
        default="http://localhost:6280/mcp", alias="DOCS_MCP_URL"
    )
    docs_mcp_auth_token: str = Field(default="", alias="DOCS_MCP_AUTH_TOKEN")
    # Hard ceiling on the docs-mcp research call. It runs in the Stage 3 submit
    # critical path; a slow/unreachable MCP server must not hang "Continue" — on
    # timeout the unknown tools stay 'none' (the conservative fallback).
    docs_mcp_research_timeout_s: float = Field(
        default=25.0, alias="DOCS_MCP_RESEARCH_TIMEOUT_S"
    )
    # When True, an unrecognized tool that isn't in the docs-mcp index is SCRAPED
    # (its latest docs indexed) before the estimate continues, instead of just
    # searching the existing index. Scraping is slow (crawl + embed), so it uses a
    # larger timeout. Requires an embeddings provider on docs-mcp-server
    # (OPENAI_API_KEY). On timeout/failure the tool still degrades to 'none'.
    docs_mcp_auto_scrape: bool = Field(default=True, alias="DOCS_MCP_AUTO_SCRAPE")
    docs_mcp_scrape_timeout_s: float = Field(
        default=240.0, alias="DOCS_MCP_SCRAPE_TIMEOUT_S"
    )
    # SSRF / prompt-injection hardening for the in-process docs-mcp research loop. The loop
    # exposes fetch_url/scrape_docs to the LLM, whose inputs derive from untrusted Stage-3 text,
    # so the backend gates every tool call. `docs_mcp_url_allowlist` is an optional comma-separated
    # list of doc domains (suffix match, e.g. "docs.anthropic.com,readthedocs.io"); when set, the
    # model may only fetch/scrape those hosts. When empty, no domain restriction applies but
    # private/loopback/link-local/metadata addresses are ALWAYS blocked. `docs_mcp_max_tool_calls`
    # caps the number of tool invocations per research call (bounds a hijacked/runaway loop).
    docs_mcp_url_allowlist: str = Field(default="", alias="DOCS_MCP_URL_ALLOWLIST")
    docs_mcp_max_tool_calls: int = Field(default=25, alias="DOCS_MCP_MAX_TOOL_CALLS")

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

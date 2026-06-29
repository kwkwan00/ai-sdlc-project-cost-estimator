"""SQLAlchemy ORM models for the Postgres persistence layer.

Three tables:
  estimate_history     — one row per completed estimate envelope (denormalized totals)
  phase_history        — one row per phase per estimate (the raw signal we calibrate from)
  calibration_aggregates — rolling per-(phase, industry, project_type, codebase-context)
                           summary the twins query during Pass 1 to anchor their LLM
                           extraction (the codebase-context code rides in the column
                           historically named maturity_level)

UUIDs are stored as String(36) so the same models also work against SQLite in tests.
Column types intentionally stay portable (no JSONB, no postgres-specific arrays) so the
migration runs on either backend.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class EstimateHistory(Base):
    __tablename__ = "estimate_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), index=True)
    # Estimation flow: "twins" (parametric) or "wbs" (bottom-up). Authoritative source for the
    # Observability per-estimate flow label (DB-side aggregation reads this column instead of
    # inferring the flow from which agents happened to be captured).
    method: Mapped[str] = mapped_column(String(16), server_default="twins")
    # Superseded by envelope_json; kept (nullable) for non-destructive back-compat.
    raw_input: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Denormalized Stage 2 + Stage 3 signals, copied here so calibration queries
    # don't need to join back to the source envelope.
    industry: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    project_type: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    engagement_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_timeline_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Final synthesized totals (filled in once the run reaches `completed`).
    total_ai_assisted_mid_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_manual_only_mid_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_hours_saved: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_cost_saved_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_cost_ai_assisted_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_cost_manual_only_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_weeks_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_weeks_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Full serialized EstimateEnvelope (model_dump(mode="json")) so the review page
    # can be redisplayed verbatim for a historical estimate — the summary columns
    # above drive the history list, this carries the complete detail.
    envelope_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    phases: Mapped[list[PhaseHistory]] = relationship(
        "PhaseHistory",
        back_populates="estimate",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class PhaseHistory(Base):
    __tablename__ = "phase_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    estimate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("estimate_history.id", ondelete="CASCADE"), index=True
    )
    phase: Mapped[str] = mapped_column(String(32), index=True)
    twin_name: Mapped[str] = mapped_column(String(64))
    algorithm: Mapped[str] = mapped_column(String(64))

    ai_assisted_optimistic: Mapped[float] = mapped_column(Float, default=0.0)
    ai_assisted_mid: Mapped[float] = mapped_column(Float, default=0.0)
    ai_assisted_pessimistic: Mapped[float] = mapped_column(Float, default=0.0)
    manual_only_optimistic: Mapped[float] = mapped_column(Float, default=0.0)
    manual_only_mid: Mapped[float] = mapped_column(Float, default=0.0)
    manual_only_pessimistic: Mapped[float] = mapped_column(Float, default=0.0)

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Now holds a codebase-context code (0–3; -1 = any); historically named maturity_level.
    maturity_level: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Denormalized for fast (phase, industry, project_type, maturity) filtering.
    industry: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    project_type: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    estimate: Mapped[EstimateHistory] = relationship("EstimateHistory", back_populates="phases")


class LlmCall(Base):
    """One persisted LLM call — the per-call grain behind the Observability page (model, agent, the
    tokens + cost, and the wall-clock timestamp). Storing each call relationally lets usage be
    aggregated in SQL (SUM / GROUP BY agent|model|estimate) instead of parsed out of every estimate's
    envelope_json blob."""

    __tablename__ = "llm_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Nullable: pre-submission agents (prefill / roster / tooling classifier) run before an estimate
    # exists, so their calls are persisted with no estimate. They still count toward the grand total
    # + per-agent breakdown; they're just absent from the per-estimate (joined) rollup.
    estimate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("estimate_history.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # The wizard-run UUID, set on pre-submission calls so they can be associated with the eventual
    # estimate once it's created (UPDATE estimate_id WHERE session_id). Null for graph/WBS calls.
    session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    agent: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(64), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    called_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )


class CalibrationAggregate(Base):
    """Rolling per-(phase, industry, project_type, codebase-context) summary.

    Refreshed by `refresh_calibration_for_phase` after estimates complete. Twins query
    rows from this table during Pass 1 to anchor their LLM extraction — e.g., the
    Discovery twin pulls the average UCP→hours mapping for fintech greenfield projects
    in a given codebase context and includes it in the prompt as a calibration hint.

    `industry` / `project_type` use empty string ("") as the "any" sentinel, and
    `maturity_level` uses -1 (0–3 are the real codebase-context codes), so the unique
    constraint stays simple (NULLs don't compare equal in unique indexes).
    """

    __tablename__ = "calibration_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "phase",
            "industry",
            "project_type",
            "maturity_level",
            name="uq_calibration_dimensions",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phase: Mapped[str] = mapped_column(String(32), index=True)
    industry: Mapped[str] = mapped_column(String(64), default="", index=True)
    project_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    # Holds a codebase-context code (0–3; -1 = any); historically named maturity_level.
    # server_default mirrors the -1 "any" sentinel (see migration 0006).
    maturity_level: Mapped[int] = mapped_column(
        Integer, default=-1, server_default="-1", index=True
    )

    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_ai_assisted_mid: Mapped[float] = mapped_column(Float, default=0.0)
    avg_manual_only_mid: Mapped[float] = mapped_column(Float, default=0.0)
    avg_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Effective AI reduction realized in the historical samples — twins use this
    # to sanity-check their maturity-cap-derived AI hours.
    avg_ai_reduction_pct: Mapped[float] = mapped_column(Float, default=0.0)

    last_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiReductionBand(Base):
    """Tunable per-(phase, tooling) AI-reduction guardrail band.

    Each row is a ``[min_reduction, max_reduction]`` fraction (0..1, NEUTRAL
    conditions) that the twin LLM's proposed reduction is clamped into for that
    phase + tooling level — "the LLM proposes within guardrails". Admin-editable so
    the estimator can be retuned without a deploy; `orchestrator/ai_acceleration.py`
    holds the same defaults as a fallback when this table is empty/unavailable.
    """

    __tablename__ = "ai_reduction_bands"
    __table_args__ = (
        UniqueConstraint("phase", "tooling_level", name="uq_reduction_band_dimensions"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phase: Mapped[str] = mapped_column(String(32), index=True)
    tooling_level: Mapped[str] = mapped_column(String(32))
    min_reduction: Mapped[float] = mapped_column(Float, default=0.0)
    max_reduction: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StaffingCoefficient(Base):
    """Tunable team-scaling coefficient (Brooks coordination overhead + the diminishing-returns
    exponent), keyed by name. Admin-editable so the staffing model can be retuned without a
    deploy; ``orchestrator/staffing.py::DEFAULT_STAFFING_COEFFS`` holds the same defaults as a
    fallback when this table is empty/unavailable."""

    __tablename__ = "staffing_coefficients"

    coefficient: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DefaultRate(Base):
    """Default hourly rate per role ``(category, seniority)`` — the org's standard rate card.
    Admin-editable on the Settings screen so rates can be retuned without a deploy;
    ``pricing.py::DEFAULT_RATES`` holds the same defaults as a fallback when this table is
    empty/unavailable. The roster agent seeds new estimates from these rates (the user can still
    override per estimate)."""

    __tablename__ = "default_rates"

    category: Mapped[str] = mapped_column(String(32), primary_key=True)
    seniority: Mapped[str] = mapped_column(String(32), primary_key=True)
    rate: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CustomRateRole(Base):
    """A named custom role on the rate card, added by an admin on top of the fixed
    ``(category, seniority)`` grid (``default_rates``). Each carries its own hourly rate plus
    category/seniority tags; the Stage 2 roster editor offers them as a catalog to prefill roster
    rows. Purely admin-managed (add/delete/edit) — there is **no** code-default seed, so an
    empty/missing table just means "no custom roles defined"."""

    __tablename__ = "custom_rate_roles"

    role_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(32))
    seniority: Mapped[str] = mapped_column(String(32))
    rate: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AppSetting(Base):
    """Generic string-valued application setting, keyed by name (e.g. ``development_sizing_method``).
    Admin-editable on the Settings screen; a code default applies when the key is absent/unavailable
    (so the table is optional — an empty/missing table just means 'use defaults')."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

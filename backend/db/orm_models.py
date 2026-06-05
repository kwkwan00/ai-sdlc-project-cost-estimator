"""SQLAlchemy ORM models for the Postgres persistence layer.

Three tables:
  estimate_history     — one row per completed estimate envelope (denormalized totals)
  phase_history        — one row per phase per estimate (the raw signal we calibrate from)
  calibration_aggregates — rolling per-(phase, industry, project_type, maturity) summary
                           the twins query during Pass 1 to anchor their LLM extraction

UUIDs are stored as String(36) so the same models also work against SQLite in tests.
Column types intentionally stay portable (no JSONB, no postgres-specific arrays) so the
migration runs on either backend.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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
    raw_input: Mapped[str] = mapped_column(Text, default="")

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
    maturity_level: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Denormalized for fast (phase, industry, project_type, maturity) filtering.
    industry: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    project_type: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    estimate: Mapped[EstimateHistory] = relationship("EstimateHistory", back_populates="phases")


class CalibrationAggregate(Base):
    """Rolling per-(phase, industry, project_type, maturity) summary.

    Refreshed by `refresh_calibration_for_phase` after estimates complete. Twins query
    rows from this table during Pass 1 to anchor their LLM extraction — e.g., the
    Discovery twin pulls the average UCP→hours mapping for fintech greenfield projects
    at Stage 3 maturity Level 2 and includes it in the prompt as a calibration hint.

    `industry` / `project_type` use empty string ("") as the "any" sentinel so the
    unique constraint stays simple (NULLs don't compare equal in unique indexes).
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
    maturity_level: Mapped[int] = mapped_column(Integer, default=0, index=True)

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

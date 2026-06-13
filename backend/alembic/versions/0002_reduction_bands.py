"""AI reduction guardrail bands

Revision ID: 0002_reduction_bands
Revises: 0001_initial
Create Date: 2026-06-11

Creates ai_reduction_bands — the per-(phase, tooling) [min, max] reduction
guardrails the twin LLM's proposed reduction is clamped into — and seeds it with
the conservative defaults that mirror
``orchestrator/ai_acceleration.py::DEFAULT_BANDS``. Admins can edit rows to retune
the estimator without a deploy; the code keeps the same values as a fallback.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_reduction_bands"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors orchestrator/ai_acceleration.py::DEFAULT_BANDS (fractions, neutral conditions).
_DEFAULT_BANDS: dict[str, dict[str, tuple[float, float]]] = {
    "discovery": {"none": (0.00, 0.00), "autocomplete": (0.00, 0.03), "chat": (0.03, 0.09), "agentic": (0.06, 0.12)},
    "ux_design": {"none": (0.00, 0.00), "autocomplete": (0.00, 0.03), "chat": (0.02, 0.07), "agentic": (0.04, 0.10)},
    "development": {"none": (0.00, 0.00), "autocomplete": (0.04, 0.09), "chat": (0.08, 0.16), "agentic": (0.12, 0.22)},
    "code_review": {"none": (0.00, 0.00), "autocomplete": (0.02, 0.05), "chat": (0.05, 0.11), "agentic": (0.08, 0.15)},
    "deployment": {"none": (0.00, 0.00), "autocomplete": (0.00, 0.04), "chat": (0.03, 0.08), "agentic": (0.05, 0.11)},
    "qa_testing": {"none": (0.00, 0.00), "autocomplete": (0.03, 0.07), "chat": (0.06, 0.12), "agentic": (0.10, 0.17)},
}


def upgrade() -> None:
    table = op.create_table(
        "ai_reduction_bands",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("tooling_level", sa.String(length=32), nullable=False),
        sa.Column("min_reduction", sa.Float, nullable=False, server_default="0"),
        sa.Column("max_reduction", sa.Float, nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("phase", "tooling_level", name="uq_reduction_band_dimensions"),
    )
    op.create_index("ix_ai_reduction_bands_phase", "ai_reduction_bands", ["phase"])
    op.bulk_insert(
        table,
        [
            {"phase": ph, "tooling_level": tl, "min_reduction": lo, "max_reduction": hi}
            for ph, by_tool in _DEFAULT_BANDS.items()
            for tl, (lo, hi) in by_tool.items()
        ],
    )


def downgrade() -> None:
    op.drop_table("ai_reduction_bands")

"""Staffing (Brooks + diminishing returns) coefficients

Revision ID: 0009_staffing_coefficients
Revises: 0008_lower_dev_chat_agentic_bands
Create Date: 2026-06-14

Creates staffing_coefficients — the key→value team-scaling coefficients (Brooks
coordination overhead + the diminishing-returns exponent) consumed by
``orchestrator/nodes/synthesize_estimate.py`` and edited on the Settings screen — and seeds
it with the defaults from ``orchestrator/staffing.py::DEFAULT_STAFFING_COEFFS``. The code
keeps the same values as a fallback when this table is empty/unavailable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_staffing_coefficients"
down_revision: str | None = "0008_lower_dev_chat_agentic_bands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors orchestrator/staffing.py::DEFAULT_STAFFING_COEFFS.
_DEFAULTS: dict[str, float] = {
    "link_cost": 0.06,
    "free_team_size": 3.0,
    "overhead_cap": 0.40,
    "diminishing_returns_exponent": 0.90,
}


def upgrade() -> None:
    table = op.create_table(
        "staffing_coefficients",
        sa.Column("coefficient", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.bulk_insert(
        table,
        [{"coefficient": k, "value": v} for k, v in _DEFAULTS.items()],
    )


def downgrade() -> None:
    op.drop_table("staffing_coefficients")

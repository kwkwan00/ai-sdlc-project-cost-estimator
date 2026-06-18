"""Default rate card (per role category × seniority)

Revision ID: 0011_default_rates
Revises: 0010_raise_dev_agentic_band
Create Date: 2026-06-17

Creates the ``default_rates`` table — the admin-editable standard hourly rate card backing the
Settings screen — and seeds it from ``pricing.py::DEFAULT_RATES`` (28 category×seniority cells,
inlined here as a snapshot). The roster agent reads these to rate new estimates' rosters;
``pricing.DEFAULT_RATES`` is the code fallback when the table is empty/unavailable. Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_default_rates"
down_revision: str | None = "0010_raise_dev_agentic_band"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (category, seniority, rate) — snapshot of pricing.DEFAULT_RATES at migration time.
_SEED: list[tuple[str, str, float]] = [
    ("product", "senior", 220.0),
    ("product", "mid", 180.0),
    ("product", "junior", 140.0),
    ("product", "other", 180.0),
    ("engineering", "senior", 240.0),
    ("engineering", "mid", 195.0),
    ("engineering", "junior", 150.0),
    ("engineering", "other", 195.0),
    ("ui_ux", "senior", 200.0),
    ("ui_ux", "mid", 165.0),
    ("ui_ux", "junior", 130.0),
    ("ui_ux", "other", 165.0),
    ("qa", "senior", 170.0),
    ("qa", "mid", 140.0),
    ("qa", "junior", 110.0),
    ("qa", "other", 140.0),
    ("devops", "senior", 230.0),
    ("devops", "mid", 190.0),
    ("devops", "junior", 150.0),
    ("devops", "other", 190.0),
    ("data", "senior", 235.0),
    ("data", "mid", 195.0),
    ("data", "junior", 150.0),
    ("data", "other", 195.0),
    ("other", "senior", 200.0),
    ("other", "mid", 165.0),
    ("other", "junior", 130.0),
    ("other", "other", 165.0),
]


def upgrade() -> None:
    op.create_table(
        "default_rates",
        sa.Column("category", sa.String(length=32), primary_key=True),
        sa.Column("seniority", sa.String(length=32), primary_key=True),
        sa.Column("rate", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.bulk_insert(
        sa.table(
            "default_rates",
            sa.column("category", sa.String),
            sa.column("seniority", sa.String),
            sa.column("rate", sa.Float),
        ),
        [{"category": c, "seniority": s, "rate": r} for c, s, r in _SEED],
    )


def downgrade() -> None:
    op.drop_table("default_rates")

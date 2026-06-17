"""Relax raw_input nullability and fix maturity_level server default

Revision ID: 0006_relax_raw_input_and_maturity_default
Revises: 0005_estimate_envelope_json
Create Date: 2026-06-12

Fixes two schema/code drifts (both non-destructive, fully reversible):

1. estimate_history.raw_input is NOT NULL but is never written anymore — it was
   superseded by envelope_json. Make it nullable so the column matches reality
   without dropping it (existing rows keep their data).
2. calibration_aggregates.maturity_level had server_default '0', but the code uses
   -1 (_ANY_MATURITY) as the "any maturity" sentinel (0–3 are real codebase-context
   codes). Align the server_default to '-1' so DB-side inserts match the sentinel.

Uses batch_alter_table so the ALTERs also apply cleanly on SQLite.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_relax_raw_input_and_maturity_default"
down_revision: str | None = "0005_estimate_envelope_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("estimate_history") as batch_op:
        batch_op.alter_column(
            "raw_input",
            existing_type=sa.Text(),
            nullable=True,
            existing_server_default="",
            server_default=None,
        )
    with op.batch_alter_table("calibration_aggregates") as batch_op:
        batch_op.alter_column(
            "maturity_level",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default="-1",
        )


def downgrade() -> None:
    with op.batch_alter_table("calibration_aggregates") as batch_op:
        batch_op.alter_column(
            "maturity_level",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default="0",
        )
    # Rows written after 0006 stopped populating raw_input (superseded by
    # envelope_json), so they hold NULL. Backfill them to '' before re-imposing
    # NOT NULL — otherwise the alter_column below fails on Postgres (and any other
    # engine that validates NOT NULL against existing data).
    op.execute("UPDATE estimate_history SET raw_input = '' WHERE raw_input IS NULL")
    with op.batch_alter_table("estimate_history") as batch_op:
        batch_op.alter_column(
            "raw_input",
            existing_type=sa.Text(),
            nullable=False,
            server_default="",
        )

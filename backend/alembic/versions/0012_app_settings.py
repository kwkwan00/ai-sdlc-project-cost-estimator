"""Generic app_settings key→value table

Revision ID: 0012_app_settings
Revises: 0011_default_rates
Create Date: 2026-06-17

Creates the ``app_settings`` table — a generic string key→value store for admin-editable
application settings (first user: ``development_sizing_method`` = cocomo | function_points). No
seed rows: an absent key means "use the code default", so the table is optional. Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_app_settings"
down_revision: str | None = "0011_default_rates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("app_settings")

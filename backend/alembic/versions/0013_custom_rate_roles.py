"""Custom rate-card roles (named roles on top of the category×seniority grid)

Revision ID: 0013_custom_rate_roles
Revises: 0012_app_settings
Create Date: 2026-06-19

Creates the ``custom_rate_roles`` table — admin-defined named roles (label + category + seniority
+ rate) that sit on top of the fixed ``default_rates`` grid. Admins add/delete them on the Settings
screen, and the Stage 2 roster editor offers them as a catalog to prefill roster rows. There is no
seed (an empty table simply means no custom roles). Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_custom_rate_roles"
down_revision: str | None = "0012_app_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_rate_roles",
        sa.Column("role_id", sa.String(length=64), primary_key=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("seniority", sa.String(length=32), nullable=False),
        sa.Column("rate", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("custom_rate_roles")

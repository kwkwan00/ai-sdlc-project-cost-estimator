"""Store the full estimate envelope for redisplay

Revision ID: 0005_estimate_envelope_json
Revises: 0004_drop_noncoding_autocomplete_bands
Create Date: 2026-06-12

Adds estimate_history.envelope_json — the full serialized EstimateEnvelope — so the
review page can be reopened verbatim for a historical estimate. Portable JSON type
(JSON on Postgres, TEXT-backed JSON on SQLite). Nullable; back-filled on the next
save of each estimate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_estimate_envelope_json"
down_revision: str | None = "0004_drop_noncoding_autocomplete_bands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "estimate_history",
        sa.Column("envelope_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("estimate_history", "envelope_json")

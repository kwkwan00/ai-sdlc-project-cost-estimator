"""Backfill estimate_history.method from envelope_json for pre-0017 rows

Revision ID: 0018_backfill_method_from_envelope
Revises: 0017_estimate_history_method
Create Date: 2026-06-28

Migration 0017 added ``estimate_history.method`` with ``server_default='twins'`` but did NOT backfill
existing rows. A WBS estimate completed before 0017 ran is terminal (never re-saved), so its ``method``
stays the default 'twins' — mislabeling it as a parametric estimate and stripping its WBS badge +
WBS-only "Duplicate" action on the dashboard. The authoritative flow still lives in the stored
``envelope_json`` blob, so this one-time data migration copies it across: any row still marked 'twins'
whose ``envelope_json.method`` is 'wbs' is corrected to 'wbs'. No-op on a fresh DB. Postgres-targeted
(the JSON ``->>`` operator); the test suite uses ``create_all``, not these migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_backfill_method_from_envelope"
down_revision: str | None = "0017_estimate_history_method"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE estimate_history
           SET method = 'wbs'
         WHERE method = 'twins'
           AND envelope_json IS NOT NULL
           AND (envelope_json ->> 'method') = 'wbs'
        """
    )


def downgrade() -> None:
    # No-op: once backfilled we can't distinguish a corrected 'wbs' row from a natively-'wbs' one,
    # and reverting would re-introduce the mislabeling. The column itself is dropped by 0017's down.
    pass

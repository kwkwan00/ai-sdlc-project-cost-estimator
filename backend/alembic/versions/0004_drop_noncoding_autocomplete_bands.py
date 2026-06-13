"""Drop autocomplete bands for non-coding phases

Revision ID: 0004_drop_noncoding_autocomplete_bands
Revises: 0003_double_reduction_bands
Create Date: 2026-06-12

Inline tab-completion is a code-writing assist, so the AUTOCOMPLETE tooling level no
longer applies to discovery, UX design, or code review — those rows are removed from
ai_reduction_bands (mirroring their removal from
``orchestrator/ai_acceleration.py::DEFAULT_BANDS``), so the bands fall back to (0, 0)
= zero reduction. Reversible (restores the doubled 0003 values).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_drop_noncoding_autocomplete_bands"
down_revision: str | None = "0003_double_reduction_bands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NON_CODING_PHASES = ("discovery", "ux_design", "code_review")
# The doubled (0003) autocomplete values, for restoring on downgrade.
_RESTORE = {"discovery": (0.00, 0.06), "ux_design": (0.00, 0.06), "code_review": (0.04, 0.10)}


def upgrade() -> None:
    for phase in _NON_CODING_PHASES:
        op.execute(
            sa.text(
                "DELETE FROM ai_reduction_bands "
                "WHERE phase = :ph AND tooling_level = 'autocomplete'"
            ).bindparams(ph=phase)
        )


def downgrade() -> None:
    for phase, (lo, hi) in _RESTORE.items():
        op.execute(
            sa.text(
                "INSERT INTO ai_reduction_bands "
                "(phase, tooling_level, min_reduction, max_reduction) "
                "VALUES (:ph, 'autocomplete', :lo, :hi)"
            ).bindparams(ph=phase, lo=lo, hi=hi)
        )

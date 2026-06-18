"""Raise the Development AGENTIC AI-reduction band

Revision ID: 0010_raise_dev_agentic_band
Revises: 0009_staffing_coefficients
Create Date: 2026-06-17

Raises the Development phase's AGENTIC reduction guardrail band from (0.36, 0.66) to
(0.45, 0.72), matching the new ``orchestrator/ai_acceleration.py::DEFAULT_BANDS`` — a less
conservative AI saving for agentic-tooled development (the dominant phase, so this is the
high-impact knob). CHAT / NONE / AUTOCOMPLETE are left as-is. Only a row still at the prior
(post-0008) value is updated (matched with a small float tolerance), so admin customizations
made via the Settings screen are left untouched. Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_raise_dev_agentic_band"
down_revision: str | None = "0009_staffing_coefficients"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRIOR: tuple[float, float] = (0.36, 0.66)
_NEW: tuple[float, float] = (0.45, 0.72)

_UPDATE = sa.text(
    "UPDATE ai_reduction_bands SET min_reduction = :nmin, max_reduction = :nmax "
    "WHERE phase = 'development' AND tooling_level = 'agentic' "
    "AND ABS(min_reduction - :omin) < 1e-6 AND ABS(max_reduction - :omax) < 1e-6"
)


def _retune(*, reverse: bool) -> None:
    frm, to = (_NEW, _PRIOR) if reverse else (_PRIOR, _NEW)
    op.get_bind().execute(
        _UPDATE.bindparams(omin=frm[0], omax=frm[1], nmin=to[0], nmax=to[1])
    )


def upgrade() -> None:
    _retune(reverse=False)


def downgrade() -> None:
    _retune(reverse=True)

"""Double the AI-reduction guardrail defaults

Revision ID: 0003_double_reduction_bands
Revises: 0002_reduction_bands
Create Date: 2026-06-12

Doubles every seeded ai_reduction_bands value to match the new
``orchestrator/ai_acceleration.py::DEFAULT_BANDS``. Only rows still at the original
0002 seed values are updated (matched with a small float tolerance), so any admin
customizations made via the Settings screen are left untouched. Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_double_reduction_bands"
down_revision: str | None = "0002_reduction_bands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The original 0002 seed values (pre-doubling).
_ORIGINAL_BANDS: dict[str, dict[str, tuple[float, float]]] = {
    "discovery": {"none": (0.00, 0.00), "autocomplete": (0.00, 0.03), "chat": (0.03, 0.09), "agentic": (0.06, 0.12)},
    "ux_design": {"none": (0.00, 0.00), "autocomplete": (0.00, 0.03), "chat": (0.02, 0.07), "agentic": (0.04, 0.10)},
    "development": {"none": (0.00, 0.00), "autocomplete": (0.04, 0.09), "chat": (0.08, 0.16), "agentic": (0.12, 0.22)},
    "code_review": {"none": (0.00, 0.00), "autocomplete": (0.02, 0.05), "chat": (0.05, 0.11), "agentic": (0.08, 0.15)},
    "deployment": {"none": (0.00, 0.00), "autocomplete": (0.00, 0.04), "chat": (0.03, 0.08), "agentic": (0.05, 0.11)},
    "qa_testing": {"none": (0.00, 0.00), "autocomplete": (0.03, 0.07), "chat": (0.06, 0.12), "agentic": (0.10, 0.17)},
}

_UPDATE = sa.text(
    "UPDATE ai_reduction_bands SET min_reduction = :nmin, max_reduction = :nmax "
    "WHERE phase = :ph AND tooling_level = :tl "
    "AND ABS(min_reduction - :omin) < 1e-6 AND ABS(max_reduction - :omax) < 1e-6"
)


def _retune(*, reverse: bool) -> None:
    bind = op.get_bind()
    for phase, by_tool in _ORIGINAL_BANDS.items():
        for tooling, (lo, hi) in by_tool.items():
            original, doubled = (lo, hi), (lo * 2, hi * 2)
            frm, to = (doubled, original) if reverse else (original, doubled)
            bind.execute(
                _UPDATE.bindparams(
                    ph=phase, tl=tooling,
                    omin=frm[0], omax=frm[1], nmin=to[0], nmax=to[1],
                )
            )


def upgrade() -> None:
    _retune(reverse=False)


def downgrade() -> None:
    _retune(reverse=True)

"""Double the Development CHAT + AGENTIC AI-reduction bands

Revision ID: 0007_double_development_chat_agentic_bands
Revises: 0006_relax_raw_input_and_maturity_default
Create Date: 2026-06-12

Doubles the Development phase's CHAT and AGENTIC reduction guardrail bands to match
the new ``orchestrator/ai_acceleration.py::DEFAULT_BANDS`` (CHAT 0.16/0.32 → 0.32/0.64,
AGENTIC 0.24/0.44 → 0.48/0.88). NONE and AUTOCOMPLETE are intentionally left as-is.
Only rows still at the prior (post-0003) seed values are updated (matched with a small
float tolerance), so any admin customizations made via the Settings screen are left
untouched. Reversible (downgrade halves them back).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_double_development_chat_agentic_bands"
down_revision: str | None = "0006_relax_raw_input_and_maturity_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The Development rows being retuned, keyed by tooling level, at their PRIOR
# (post-0003) values. Only CHAT and AGENTIC are doubled.
_PRIOR_BANDS: dict[str, tuple[float, float]] = {
    "chat": (0.16, 0.32),
    "agentic": (0.24, 0.44),
}

_UPDATE = sa.text(
    "UPDATE ai_reduction_bands SET min_reduction = :nmin, max_reduction = :nmax "
    "WHERE phase = 'development' AND tooling_level = :tl "
    "AND ABS(min_reduction - :omin) < 1e-6 AND ABS(max_reduction - :omax) < 1e-6"
)


def _retune(*, reverse: bool) -> None:
    bind = op.get_bind()
    for tooling, (lo, hi) in _PRIOR_BANDS.items():
        prior, doubled = (lo, hi), (lo * 2, hi * 2)
        frm, to = (doubled, prior) if reverse else (prior, doubled)
        bind.execute(
            _UPDATE.bindparams(
                tl=tooling, omin=frm[0], omax=frm[1], nmin=to[0], nmax=to[1]
            )
        )


def upgrade() -> None:
    _retune(reverse=False)


def downgrade() -> None:
    _retune(reverse=True)

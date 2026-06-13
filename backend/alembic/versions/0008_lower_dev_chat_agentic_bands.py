"""Lower the Development CHAT + AGENTIC AI-reduction bands to 75%

Revision ID: 0008_lower_dev_chat_agentic_bands
Revises: 0007_double_development_chat_agentic_bands
Create Date: 2026-06-12

Scales the Development phase's CHAT and AGENTIC reduction guardrail bands to 75% of
their current (post-0007) values, matching the new
``orchestrator/ai_acceleration.py::DEFAULT_BANDS`` (CHAT 0.32/0.64 → 0.24/0.48,
AGENTIC 0.48/0.88 → 0.36/0.66). NONE and AUTOCOMPLETE are left as-is. Only rows still
at the prior (post-0007) values are updated (matched with a small float tolerance), so
admin customizations made via the Settings screen are left untouched. Reversible
(downgrade restores the prior values by dividing by 0.75).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_lower_dev_chat_agentic_bands"
down_revision: str | None = "0007_double_development_chat_agentic_bands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCALE = 0.75

# The Development rows being retuned, keyed by tooling level, at their PRIOR
# (post-0007) values. Only CHAT and AGENTIC are scaled.
_PRIOR_BANDS: dict[str, tuple[float, float]] = {
    "chat": (0.32, 0.64),
    "agentic": (0.48, 0.88),
}

_UPDATE = sa.text(
    "UPDATE ai_reduction_bands SET min_reduction = :nmin, max_reduction = :nmax "
    "WHERE phase = 'development' AND tooling_level = :tl "
    "AND ABS(min_reduction - :omin) < 1e-6 AND ABS(max_reduction - :omax) < 1e-6"
)


def _retune(*, reverse: bool) -> None:
    bind = op.get_bind()
    for tooling, (lo, hi) in _PRIOR_BANDS.items():
        prior, scaled = (lo, hi), (round(lo * _SCALE, 6), round(hi * _SCALE, 6))
        frm, to = (scaled, prior) if reverse else (prior, scaled)
        bind.execute(
            _UPDATE.bindparams(
                tl=tooling, omin=frm[0], omax=frm[1], nmin=to[0], nmax=to[1]
            )
        )


def upgrade() -> None:
    _retune(reverse=False)


def downgrade() -> None:
    _retune(reverse=True)

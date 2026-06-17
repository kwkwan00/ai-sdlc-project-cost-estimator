"""AI-reduction guardrail bands: per-(phase, tooling_level) [min, max] reduction
fractions, read and upserted by the Settings admin screen.

`get_reduction_bands()` returns the DB overrides as a nested dict; callers merge them
over the in-code `DEFAULT_BANDS` (orchestrator/ai_acceleration.py) when the DB is
empty/disabled. `upsert_reduction_bands(...)` persists edits and reports whether the
write landed so the UI can warn when Postgres is disabled.

Both functions honor the never-raise persistence contract: DB errors are caught,
logged, and converted to the empty/False case.
"""

from __future__ import annotations

import logging

from db.orm_models import AiReductionBand
from db.repositories._common import fetch_all_rows, upsert_keyed

logger = logging.getLogger(__name__)


async def get_reduction_bands() -> dict[str, dict[str, list[float]]]:
    """Return DB-stored AI-reduction guardrail bands as nested
    ``{phase: {tooling_level: [min, max]}}``.

    Returns an empty dict when Postgres is disabled or the table is empty/unreadable
    — callers (the twins, via parse_input → state) then fall back to the in-code
    ``DEFAULT_BANDS`` in orchestrator/ai_acceleration.py.
    """
    rows = await fetch_all_rows(AiReductionBand)
    out: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        out.setdefault(r.phase, {})[r.tooling_level] = [r.min_reduction, r.max_reduction]
    return out


def _apply_band(row: AiReductionBand, item: tuple[str, str, float, float]) -> None:
    _phase, _tooling, lo, hi = item
    row.min_reduction = lo
    row.max_reduction = hi


def _make_band(item: tuple[str, str, float, float]) -> AiReductionBand:
    phase, tooling, lo, hi = item
    return AiReductionBand(
        phase=phase,
        tooling_level=tooling,
        min_reduction=lo,
        max_reduction=hi,
    )


async def upsert_reduction_bands(
    items: list[tuple[str, str, float, float]],
) -> bool:
    """Upsert per-(phase, tooling_level) AI-reduction bands (fractions 0..1).

    `items` are ``(phase, tooling_level, min_reduction, max_reduction)``. Each row is
    updated in place or inserted, keyed on (phase, tooling_level). Returns True when
    persisted, False when Postgres is disabled or the write fails — the admin endpoint
    surfaces that so the UI can warn the change wasn't saved.
    """
    return await upsert_keyed(
        AiReductionBand,
        items,
        key_of=lambda item: (item[0], item[1]),
        key_of_row=lambda r: (r.phase, r.tooling_level),
        apply=_apply_band,
        make=_make_band,
    )

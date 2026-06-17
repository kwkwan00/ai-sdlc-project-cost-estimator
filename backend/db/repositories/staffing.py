"""Team-scaling (Brooks + diminishing returns) coefficients — a small key→value table read
and upserted by the Settings admin screen.

`get_staffing_coefficients()` returns the DB overrides as a flat ``{key: value}`` dict; callers
(`synthesize_estimate`, the admin helper) merge them over the in-code ``DEFAULT_STAFFING_COEFFS``
(orchestrator/staffing.py) — the model's per-key fallback fills any missing keys.
`upsert_staffing_coefficients(...)` persists edits and reports whether the write landed so the UI
can warn when Postgres is disabled.

Both functions honor the never-raise persistence contract: DB errors are caught, logged, and
converted to the empty/False case.
"""

from __future__ import annotations

import logging

from db.orm_models import StaffingCoefficient
from db.repositories._common import fetch_all_rows, upsert_keyed

logger = logging.getLogger(__name__)


async def get_staffing_coefficients() -> dict[str, float]:
    """Return DB-stored staffing-model coefficient overrides as ``{key: value}``.

    Empty dict when Postgres is disabled or the table is empty/unreadable — callers fall back
    to ``DEFAULT_STAFFING_COEFFS`` (per-key) in orchestrator/staffing.py.
    """
    rows = await fetch_all_rows(StaffingCoefficient)
    return {r.coefficient: r.value for r in rows}


def _apply_coeff(row: StaffingCoefficient, item: tuple[str, float]) -> None:
    _key, value = item
    row.value = value


def _make_coeff(item: tuple[str, float]) -> StaffingCoefficient:
    key, value = item
    return StaffingCoefficient(coefficient=key, value=value)


async def upsert_staffing_coefficients(items: list[tuple[str, float]]) -> bool:
    """Upsert ``(coefficient, value)`` rows, keyed on the coefficient name. Returns True when
    persisted, False when Postgres is disabled or the write fails — the admin endpoint surfaces
    that so the UI can warn the change wasn't saved."""
    return await upsert_keyed(
        StaffingCoefficient,
        items,
        key_of=lambda item: item[0],
        key_of_row=lambda r: r.coefficient,
        apply=_apply_coeff,
        make=_make_coeff,
    )

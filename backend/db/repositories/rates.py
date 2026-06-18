"""Default rate card — a ``(category, seniority) → rate`` table read and upserted by the Settings
admin screen. Mirrors ``db/repositories/staffing.py``.

``get_default_rates()`` returns the DB overrides as ``{(RoleCategory, RoleSeniority): rate}``;
callers (the rate-card admin, the roster agent) merge them over ``pricing.DEFAULT_RATES`` — the
code default fills any missing cell. ``upsert_default_rates(...)`` persists edits and reports
whether the write landed so the UI can warn when Postgres is disabled.

Both honor the never-raise persistence contract (via the ``_common`` helpers).
"""

from __future__ import annotations

import logging

from db.orm_models import DefaultRate
from db.repositories._common import fetch_all_rows, upsert_keyed
from models.twin_outputs import RoleCategory, RoleSeniority

logger = logging.getLogger(__name__)


async def get_default_rates() -> dict[tuple[RoleCategory, RoleSeniority], float]:
    """DB-stored rate-card overrides as ``{(category, seniority): rate}``. Empty when Postgres is
    disabled / the table is empty / unreadable; a row with an unrecognized enum value is skipped.
    Callers fall back to ``pricing.DEFAULT_RATES`` per cell."""
    out: dict[tuple[RoleCategory, RoleSeniority], float] = {}
    for r in await fetch_all_rows(DefaultRate):
        try:
            out[(RoleCategory(r.category), RoleSeniority(r.seniority))] = r.rate
        except ValueError:
            logger.warning("skipping unrecognized rate-card row %s/%s", r.category, r.seniority)
    return out


def _apply_rate(row: DefaultRate, item: tuple[RoleCategory, RoleSeniority, float]) -> None:
    row.rate = item[2]


def _make_rate(item: tuple[RoleCategory, RoleSeniority, float]) -> DefaultRate:
    cat, sen, rate = item
    return DefaultRate(category=cat.value, seniority=sen.value, rate=rate)


async def upsert_default_rates(items: list[tuple[RoleCategory, RoleSeniority, float]]) -> bool:
    """Upsert ``(category, seniority, rate)`` rows, keyed on ``(category, seniority)``. Returns
    True when persisted, False when Postgres is disabled or the write fails."""
    return await upsert_keyed(
        DefaultRate,
        items,
        key_of=lambda item: (item[0].value, item[1].value),
        key_of_row=lambda r: (r.category, r.seniority),
        apply=_apply_rate,
        make=_make_rate,
    )

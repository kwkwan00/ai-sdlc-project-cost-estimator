"""Admin surface for the default rate card (hourly rate per role ``category × seniority``).

Exposes the rate card as editable rows, merging the in-code ``pricing.DEFAULT_RATES`` with any
Postgres overrides. Writes degrade gracefully: when Postgres is disabled the GET still returns the
code defaults (read-only) and the PUT reports it wasn't persisted (``editable=false``). Mirrors
``staffing_admin.py``.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from config import get_settings
from db.repositories import get_default_rates, upsert_default_rates
from models.twin_outputs import RoleCategory, RoleSeniority
from pricing import DEFAULT_RATES, RATE_BOUNDS

logger = logging.getLogger(__name__)


class RateRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    seniority: str
    rate: float
    default_rate: float
    is_override: bool


class RateCardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # True when Postgres is connected and edits will persist.
    editable: bool
    min_rate: float
    max_rate: float
    rates: list[RateRow]


class RateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    seniority: str
    rate: float


class RateCardUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rates: list[RateInput]


async def get_effective_rates() -> RateCardResponse:
    """Merge code defaults with DB overrides into editable rate rows (all 28 cells)."""
    overrides = await get_default_rates()  # {(category, seniority): rate}, {} when off
    rows = [
        RateRow(
            category=cat.value,
            seniority=sen.value,
            rate=overrides.get((cat, sen), default),
            default_rate=default,
            is_override=(cat, sen) in overrides,
        )
        for (cat, sen), default in DEFAULT_RATES.items()
    ]
    lo, hi = RATE_BOUNDS
    return RateCardResponse(
        editable=get_settings().postgres_enabled, min_rate=lo, max_rate=hi, rates=rows
    )


async def update_rates(update: RateCardUpdate) -> RateCardResponse:
    """Validate + persist edited rates, then return the new effective state."""
    lo, hi = RATE_BOUNDS
    items: list[tuple[RoleCategory, RoleSeniority, float]] = []
    for r in update.rates:
        try:
            cat, sen = RoleCategory(r.category), RoleSeniority(r.seniority)
        except ValueError as exc:
            raise HTTPException(422, f"Unknown role cell {r.category!r}/{r.seniority!r}") from exc
        if not lo <= r.rate <= hi:
            raise HTTPException(422, f"rate for {cat.value}/{sen.value} must be in [{lo}, {hi}] (got {r.rate})")
        items.append((cat, sen, r.rate))

    persisted = await upsert_default_rates(items)
    if not persisted:
        logger.warning("rate-card update not persisted (Postgres disabled/failed)")
    return await get_effective_rates()

"""Admin surface for the global contingency management reserve %.

A single numeric app setting (``contingency_pct`` in the ``app_settings`` KV table) — a deliberate
management buffer that ``synthesize_estimate`` uplifts the final cost + timeline by (hours /
headcount unchanged). Stored as a stringified float; default 0 (no contingency). Writes degrade
gracefully: when Postgres is disabled the GET returns the code default read-only and the PUT
reports it wasn't persisted (``editable=false``). Mirrors the other admin surfaces.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from config import get_settings
from db.repositories import get_app_setting, set_app_setting

logger = logging.getLogger(__name__)

_SETTING_KEY = "contingency_pct"
DEFAULT_CONTINGENCY_PCT = 0.0
CONTINGENCY_BOUNDS = (0.0, 100.0)


class ContingencyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # True when Postgres is connected and edits will persist.
    editable: bool
    contingency_pct: float
    default_pct: float
    min_pct: float
    max_pct: float


class ContingencyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contingency_pct: float


async def get_contingency() -> ContingencyResponse:
    """The current contingency reserve % (DB → 0 default), plus the editable bounds."""
    raw = await get_app_setting(_SETTING_KEY, str(DEFAULT_CONTINGENCY_PCT))
    try:
        pct = float(raw)
    except (ValueError, TypeError):
        pct = DEFAULT_CONTINGENCY_PCT
    lo, hi = CONTINGENCY_BOUNDS
    return ContingencyResponse(
        editable=get_settings().postgres_enabled,
        contingency_pct=pct,
        default_pct=DEFAULT_CONTINGENCY_PCT,
        min_pct=lo,
        max_pct=hi,
    )


async def update_contingency(update: ContingencyUpdate) -> ContingencyResponse:
    """Validate + persist the contingency reserve %, then return the new effective state."""
    lo, hi = CONTINGENCY_BOUNDS
    if not lo <= update.contingency_pct <= hi:
        raise HTTPException(
            422, f"contingency_pct must be in [{lo}, {hi}] (got {update.contingency_pct})"
        )
    persisted = await set_app_setting(_SETTING_KEY, str(update.contingency_pct))
    if not persisted:
        logger.warning("contingency update not persisted (Postgres disabled/failed)")
    return await get_contingency()

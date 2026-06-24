"""Admin surface for the team-scaling (Brooks's Law + diminishing returns) coefficients.

Exposes the staffing-model coefficients as editable rows, merging the in-code
``DEFAULT_STAFFING_COEFFS`` with any Postgres overrides. Writes degrade gracefully: when
Postgres is disabled the GET still returns the code defaults (read-only) and the PUT reports it
wasn't persisted.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from config import get_settings
from db.repositories import get_staffing_coefficients, upsert_staffing_coefficients
from orchestrator.staffing import DEFAULT_STAFFING_COEFFS, STAFFING_COEFF_BOUNDS

logger = logging.getLogger(__name__)


class StaffingCoefficientRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: float
    default_value: float
    min_value: float
    max_value: float
    is_override: bool


class StaffingCoefficientsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # True when Postgres is connected and edits will persist.
    editable: bool
    coefficients: list[StaffingCoefficientRow]


class StaffingCoefficientInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: float


class StaffingCoefficientsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coefficients: list[StaffingCoefficientInput]


async def get_effective_staffing() -> StaffingCoefficientsResponse:
    """Merge code defaults with DB overrides into editable coefficient rows."""
    overrides = await get_staffing_coefficients()  # {key: value}, {} when off
    rows = [
        StaffingCoefficientRow(
            key=key,
            value=overrides.get(key, default),
            default_value=default,
            min_value=STAFFING_COEFF_BOUNDS[key][0],
            max_value=STAFFING_COEFF_BOUNDS[key][1],
            is_override=key in overrides,
        )
        for key, default in DEFAULT_STAFFING_COEFFS.items()
    ]
    return StaffingCoefficientsResponse(
        editable=get_settings().postgres_enabled, coefficients=rows
    )


async def update_staffing(update: StaffingCoefficientsUpdate) -> StaffingCoefficientsResponse:
    """Validate + persist edited coefficients, then return the new effective state."""
    items: list[tuple[str, float]] = []
    for c in update.coefficients:
        if c.key not in DEFAULT_STAFFING_COEFFS:
            raise HTTPException(422, f"Unknown staffing coefficient {c.key!r}")
        lo, hi = STAFFING_COEFF_BOUNDS[c.key]
        if not lo <= c.value <= hi:
            raise HTTPException(422, f"{c.key} must be in [{lo}, {hi}] (got {c.value})")
        items.append((c.key, c.value))

    persisted = await upsert_staffing_coefficients(items)
    if not persisted:
        logger.warning("staffing-coefficient update not persisted (Postgres disabled/failed)")
    return await get_effective_staffing()

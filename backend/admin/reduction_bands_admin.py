"""Admin surface for the AI-reduction guardrail bands (Settings screen).

Exposes the per-(phase × tooling) ``[min, max]`` reduction bands — the "AI-assisted
multipliers" — as editable percentages, merging the in-code ``DEFAULT_BANDS`` with any
Postgres overrides. Writes degrade gracefully: when Postgres is disabled the GET still
returns the code defaults (read-only) and the PUT reports it wasn't persisted.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from config import get_settings
from db.repositories import get_reduction_bands, upsert_reduction_bands
from orchestrator.ai_acceleration import default_bands

logger = logging.getLogger(__name__)


class ReductionBandRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str
    tooling_level: str
    min_pct: float
    max_pct: float
    default_min_pct: float
    default_max_pct: float
    is_override: bool


class ReductionBandsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # True when Postgres is connected and edits will persist.
    editable: bool
    bands: list[ReductionBandRow]


class ReductionBandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str
    tooling_level: str
    min_pct: float = Field(ge=0, le=100)
    max_pct: float = Field(ge=0, le=100)


class ReductionBandsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bands: list[ReductionBandInput]


async def get_effective_bands() -> ReductionBandsResponse:
    """Merge code defaults with DB overrides into editable percentage rows."""
    overrides = await get_reduction_bands()  # {phase: {tooling: [lo, hi]}}, {} when off
    rows: list[ReductionBandRow] = []
    for phase, tooling, dlo, dhi in default_bands():
        cell = overrides.get(phase.value, {}).get(tooling.value)
        lo, hi = (cell[0], cell[1]) if cell and len(cell) == 2 else (dlo, dhi)
        rows.append(
            ReductionBandRow(
                phase=phase.value,
                tooling_level=tooling.value,
                min_pct=round(lo * 100, 1),
                max_pct=round(hi * 100, 1),
                default_min_pct=round(dlo * 100, 1),
                default_max_pct=round(dhi * 100, 1),
                is_override=cell is not None,
            )
        )
    return ReductionBandsResponse(
        editable=get_settings().postgres_enabled, bands=rows
    )


async def update_bands(update: ReductionBandsUpdate) -> ReductionBandsResponse:
    """Validate + persist edited bands, then return the new effective state."""
    valid_cells = {(p.value, t.value) for p, t, _lo, _hi in default_bands()}
    items: list[tuple[str, str, float, float]] = []
    for b in update.bands:
        if (b.phase, b.tooling_level) not in valid_cells:
            raise HTTPException(
                422, f"Unknown band {b.phase}/{b.tooling_level} (NONE is not editable)"
            )
        if b.min_pct > b.max_pct:
            raise HTTPException(
                422,
                f"{b.phase}/{b.tooling_level}: min ({b.min_pct}%) must be ≤ max ({b.max_pct}%)",
            )
        items.append((b.phase, b.tooling_level, b.min_pct / 100, b.max_pct / 100))

    persisted = await upsert_reduction_bands(items)
    if not persisted:
        logger.warning("reduction-band update not persisted (Postgres disabled/failed)")
    return await get_effective_bands()

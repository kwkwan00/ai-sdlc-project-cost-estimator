"""Admin endpoints for the Settings screen.

Read and update the AI-reduction guardrail bands (code defaults merged with DB
overrides). When Postgres is disabled the update is not persisted and the response's
`editable` flag is false so the UI can warn the change wasn't saved.
"""

from __future__ import annotations

from fastapi import APIRouter

from reduction_bands_admin import (
    ReductionBandsResponse,
    ReductionBandsUpdate,
    get_effective_bands,
    update_bands,
)
from staffing_admin import (
    StaffingCoefficientsResponse,
    StaffingCoefficientsUpdate,
    get_effective_staffing,
    update_staffing,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/reduction-bands", response_model=ReductionBandsResponse)
async def read_reduction_bands() -> ReductionBandsResponse:
    """Current AI-reduction guardrail bands (code defaults merged with DB overrides),
    as editable percentages — backs the Settings screen."""
    return await get_effective_bands()


@router.put("/reduction-bands", response_model=ReductionBandsResponse)
async def write_reduction_bands(req: ReductionBandsUpdate) -> ReductionBandsResponse:
    """Persist edited AI-reduction bands and return the new effective state. When
    Postgres is disabled the change is not saved (the response's `editable` is false)."""
    return await update_bands(req)


@router.get("/staffing-coefficients", response_model=StaffingCoefficientsResponse)
async def read_staffing_coefficients() -> StaffingCoefficientsResponse:
    """Current team-scaling coefficients (code defaults merged with DB overrides) — backs the
    Settings screen's Brooks's-Law / diminishing-returns section."""
    return await get_effective_staffing()


@router.put("/staffing-coefficients", response_model=StaffingCoefficientsResponse)
async def write_staffing_coefficients(
    req: StaffingCoefficientsUpdate,
) -> StaffingCoefficientsResponse:
    """Persist edited team-scaling coefficients and return the new effective state. When
    Postgres is disabled the change is not saved (the response's `editable` is false)."""
    return await update_staffing(req)

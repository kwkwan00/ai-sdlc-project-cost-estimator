"""Admin reduction-bands surface: effective-band merging, validation, endpoints.

The DB-override merge path is covered in test_postgres_layer.py (needs the aiosqlite
fixture); here we cover the Postgres-disabled defaults path + validation + routes."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import db.postgres_adapter as postgres_adapter
from reduction_bands_admin import (
    ReductionBandInput,
    ReductionBandsUpdate,
    get_effective_bands,
    update_bands,
)


@pytest.mark.asyncio
async def test_effective_bands_default_shape() -> None:
    postgres_adapter._reset_for_tests()
    resp = await get_effective_bands()
    # 18 minus the 3 autocomplete cells that don't apply (discovery, ux_design,
    # code_review) = 15. NONE is always excluded.
    assert len(resp.bands) == 15
    assert all(b.tooling_level != "none" for b in resp.bands)
    # Autocomplete only appears for code-writing phases.
    auto_phases = {b.phase for b in resp.bands if b.tooling_level == "autocomplete"}
    assert auto_phases == {"development", "deployment", "qa_testing"}
    dev = next(
        b for b in resp.bands
        if b.phase == "development" and b.tooling_level == "agentic"
    )
    assert (dev.min_pct, dev.max_pct) == (36.0, 66.0)
    assert dev.is_override is False


@pytest.mark.asyncio
async def test_update_rejects_min_above_max() -> None:
    with pytest.raises(HTTPException) as exc:
        await update_bands(
            ReductionBandsUpdate(
                bands=[
                    ReductionBandInput(
                        phase="development", tooling_level="agentic",
                        min_pct=30, max_pct=10,
                    )
                ]
            )
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_rejects_none_tooling_level() -> None:
    # NONE is not editable — an override there would wrongly grant a reduction.
    with pytest.raises(HTTPException):
        await update_bands(
            ReductionBandsUpdate(
                bands=[
                    ReductionBandInput(
                        phase="development", tooling_level="none",
                        min_pct=0, max_pct=0,
                    )
                ]
            )
        )


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def test_get_endpoint_returns_bands() -> None:
    res = _client().get("/admin/reduction-bands")
    assert res.status_code == 200
    body = res.json()
    assert len(body["bands"]) == 15
    assert "editable" in body


def test_put_endpoint_rejects_invalid_range() -> None:
    res = _client().put(
        "/admin/reduction-bands",
        json={
            "bands": [
                {"phase": "development", "tooling_level": "agentic",
                 "min_pct": 50, "max_pct": 10}
            ]
        },
    )
    assert res.status_code == 422


def test_put_endpoint_rejects_out_of_bounds_pct() -> None:
    # min_pct > 100 fails the pydantic Field(ge=0, le=100) bound (422 from FastAPI).
    res = _client().put(
        "/admin/reduction-bands",
        json={
            "bands": [
                {"phase": "development", "tooling_level": "agentic",
                 "min_pct": 150, "max_pct": 160}
            ]
        },
    )
    assert res.status_code == 422

"""Default rate-card surface: pricing.resolve_rate, effective-rate merging, validation, endpoints.

The DB-override merge path is covered in test_postgres_layer.py (needs the aiosqlite fixture);
here we cover pricing.resolve_rate, the Postgres-disabled defaults path + validation + routes."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import db.postgres_adapter as postgres_adapter
from models.twin_outputs import RoleCategory as RC
from models.twin_outputs import RoleSeniority as RS
from pricing import DEFAULT_RATES, resolve_rate
from rate_card_admin import RateCardUpdate, RateInput, get_effective_rates, update_rates


def test_resolve_rate_override_then_default() -> None:
    # Code default when no override is supplied.
    assert resolve_rate(RC.ENGINEERING, RS.SENIOR) == DEFAULT_RATES[(RC.ENGINEERING, RS.SENIOR)]
    # A DB override for a cell wins.
    assert resolve_rate(RC.ENGINEERING, RS.SENIOR, {(RC.ENGINEERING, RS.SENIOR): 999.0}) == 999.0
    # A cell absent from the overrides falls back to the code default.
    assert (
        resolve_rate(RC.QA, RS.JUNIOR, {(RC.ENGINEERING, RS.SENIOR): 999.0})
        == DEFAULT_RATES[(RC.QA, RS.JUNIOR)]
    )


def test_default_rate_card_completeness_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import pricing

    # Anchor cells match RoleRoster.default() so a vanilla proposal round-trips with same rates.
    assert resolve_rate(RC.ENGINEERING, RS.SENIOR) == 240.0
    assert resolve_rate(RC.PRODUCT, RS.JUNIOR) == 140.0
    # Every category × seniority cell is covered (no zero/missing rate).
    assert len(DEFAULT_RATES) == 28
    assert all(resolve_rate(cat, sen) > 0 for cat in RC for sen in RS)
    # The fallback is defensive for future enum additions: empty the table → RATE_FALLBACK.
    monkeypatch.setattr(pricing, "DEFAULT_RATES", {})
    assert resolve_rate(RC.QA, RS.MID) == pricing.RATE_FALLBACK


@pytest.mark.asyncio
async def test_effective_rates_default_shape() -> None:
    postgres_adapter._reset_for_tests()  # Postgres disabled → no overrides
    resp = await get_effective_rates()
    assert len(resp.rates) == 28  # 7 categories × 4 seniorities
    eng = next(r for r in resp.rates if r.category == "engineering" and r.seniority == "senior")
    assert (eng.rate, eng.default_rate, eng.is_override) == (240.0, 240.0, False)
    assert (resp.min_rate, resp.max_rate) == (0.0, 1000.0)


@pytest.mark.asyncio
async def test_update_rejects_out_of_bounds_rate() -> None:
    with pytest.raises(HTTPException) as exc:
        await update_rates(
            RateCardUpdate(rates=[RateInput(category="engineering", seniority="senior", rate=5000.0)])
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_rejects_unknown_role_cell() -> None:
    with pytest.raises(HTTPException):
        await update_rates(
            RateCardUpdate(rates=[RateInput(category="wizard", seniority="senior", rate=200.0)])
        )


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def test_get_endpoint_returns_rates() -> None:
    res = _client().get("/admin/default-rates")
    assert res.status_code == 200
    body = res.json()
    assert len(body["rates"]) == 28
    assert "editable" in body


def test_put_endpoint_rejects_negative_rate() -> None:
    res = _client().put(
        "/admin/default-rates",
        json={"rates": [{"category": "engineering", "seniority": "senior", "rate": -10.0}]},
    )
    assert res.status_code == 422

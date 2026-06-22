"""Contingency management-reserve admin surface (GET/PUT /admin/contingency).

Covers the effective-value read (DB → 0 default), bounds validation, the never-raise
disabled-Postgres path, and the routes. The app_settings KV round-trip + the synthesize-level
cost/timeline uplift are covered in test_dev_sizing.py and test_orchestrator_nodes.py respectively.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import db.postgres_adapter as postgres_adapter
from contingency_admin import (
    CONTINGENCY_BOUNDS,
    DEFAULT_CONTINGENCY_PCT,
    ContingencyUpdate,
    get_contingency,
    resolve_contingency_pct,
    update_contingency,
)


def test_resolve_contingency_pct_parses_floors_and_ceils() -> None:
    # The single read path shared by the admin GET, parse_input, and the WBS rollup: parse + clamp.
    assert resolve_contingency_pct("12.5") == 12.5
    assert resolve_contingency_pct(30.0) == 30.0
    assert resolve_contingency_pct("-5") == 0.0       # floor at 0
    assert resolve_contingency_pct("250") == 100.0    # ceil at 100 (legacy/hand-edited row)
    assert resolve_contingency_pct("nonsense") == DEFAULT_CONTINGENCY_PCT
    assert resolve_contingency_pct(None) == DEFAULT_CONTINGENCY_PCT


@pytest.mark.asyncio
async def test_contingency_default_shape() -> None:
    postgres_adapter._reset_for_tests()  # Postgres disabled → code default, read-only
    resp = await get_contingency()
    assert resp.contingency_pct == DEFAULT_CONTINGENCY_PCT == 0.0
    assert (resp.min_pct, resp.max_pct) == CONTINGENCY_BOUNDS
    assert resp.editable is False


@pytest.mark.asyncio
async def test_contingency_rejects_out_of_bounds() -> None:
    for bad in (-5.0, 250.0):
        with pytest.raises(HTTPException) as exc:
            await update_contingency(ContingencyUpdate(contingency_pct=bad))
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_contingency_update_no_ops_when_postgres_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    postgres_adapter._reset_for_tests()
    monkeypatch.setattr(postgres_adapter, "get_sessionmaker", lambda: None)
    resp = await update_contingency(ContingencyUpdate(contingency_pct=15.0))
    # A valid value is accepted but not persisted → still reports the default, read-only.
    assert resp.editable is False
    assert resp.contingency_pct == 0.0


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def test_get_endpoint_returns_contingency() -> None:
    res = _client().get("/admin/contingency")
    assert res.status_code == 200
    body = res.json()
    assert "contingency_pct" in body
    assert [body["min_pct"], body["max_pct"]] == list(CONTINGENCY_BOUNDS)


def test_put_endpoint_rejects_out_of_bounds() -> None:
    res = _client().put("/admin/contingency", json={"contingency_pct": 500.0})
    assert res.status_code == 422

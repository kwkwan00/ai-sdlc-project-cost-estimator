"""Selectable Development sizing methods (Function Points + COSMIC FP) + the app_settings KV store
+ admin surface.

Covers: ``resolve_fp`` / ``compute_fp_hours`` and ``resolve_cfp`` / ``compute_cosmic_hours`` (both
linear, no scale diseconomy), the method branch in ``build_phase_estimate`` (algorithm label + ai =
manual·(1−r) identity), the method-agnostic ``_aggregate_dev`` ensemble fold, ``get/set_app_setting``
round-trip + defaults, and the ``/admin/development-sizing-method`` GET/PUT endpoints (validation +
disabled-Postgres semantics).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import db.postgres_adapter as postgres_adapter
from db.orm_models import Base
from db.repositories import get_app_setting, set_app_setting
from dev_sizing_admin import (
    DevSizingUpdate,
    get_dev_sizing_method,
    update_dev_sizing_method,
)
from models.project_schema import RoleRoster
from models.twin_outputs import PhaseEstimate
from orchestrator.montecarlo import make_rng
from orchestrator.nodes.development_architect import (
    CFP_PER_FP,
    DEFAULT_DEV_SIZING_METHOD,
    HOURS_PER_CFP,
    HOURS_PER_FP,
    DevCOCOMOInputs,
    _aggregate_dev,
    build_phase_estimate,
    compute_cocomo_hours,
    compute_cosmic_hours,
    compute_fp_hours,
    resolve_cfp,
    resolve_fp,
)


def _inputs(**kw: Any) -> DevCOCOMOInputs:
    base: dict[str, Any] = dict(
        sloc_estimate=20000.0, primary_language="typescript", confidence=0.7
    )
    base.update(kw)
    return DevCOCOMOInputs(**base)


# ---------- FP sizing math ----------


def test_resolve_fp_prefers_explicit_then_converts_sloc() -> None:
    assert resolve_fp(_inputs(function_points=300.0)) == 300.0
    # No FP given → convert SLOC via the language ratio (typescript: 47 SLOC/FP).
    assert resolve_fp(_inputs(sloc_estimate=4700.0, function_points=None)) == pytest.approx(100.0)
    # Neither → small last-resort default.
    assert resolve_fp(_inputs(sloc_estimate=None, function_points=None)) == 100.0


def test_compute_fp_hours_is_linear_in_size() -> None:
    one = compute_fp_hours(_inputs(function_points=200.0))[0]
    two = compute_fp_hours(_inputs(function_points=400.0))[0]
    # Linear: doubling FP doubles hours (no KSLOC^E diseconomy).
    assert two == pytest.approx(2 * one)


def test_compute_fp_hours_breakdown_and_modifiers() -> None:
    hours, bd = compute_fp_hours(_inputs(function_points=100.0, eaf_composite=1.0))
    assert bd["function_points"] == 100.0 and bd["hours_per_fp"] == HOURS_PER_FP
    assert hours == pytest.approx(100.0 * HOURS_PER_FP)  # eaf=1, modern_web stack=1, leverage=0


def test_fp_and_cocomo_diverge_on_large_size() -> None:
    # COCOMO's scale exponent makes it superlinear; FP stays linear, so they differ at scale.
    big = _inputs(function_points=2000.0, sloc_estimate=94000.0)
    assert compute_fp_hours(big)[0] != pytest.approx(compute_cocomo_hours(big)[0])


# ---------- COSMIC FP sizing math ----------


def test_resolve_cfp_prefers_explicit_then_scales_fp() -> None:
    assert resolve_cfp(_inputs(cosmic_cfp=240.0)) == 240.0
    # No CFP → scale the IFPUG FP count (explicit) by CFP_PER_FP.
    assert resolve_cfp(_inputs(function_points=200.0, sloc_estimate=None, cosmic_cfp=None)) == (
        pytest.approx(200.0 * CFP_PER_FP)
    )
    # No CFP, no FP → SLOC→FP→CFP (typescript 47 SLOC/FP): 4700/47 = 100 FP → 100·CFP_PER_FP.
    assert resolve_cfp(
        _inputs(sloc_estimate=4700.0, function_points=None, cosmic_cfp=None)
    ) == pytest.approx(100.0 * CFP_PER_FP)


def test_compute_cosmic_hours_is_linear_and_breakdown() -> None:
    one = compute_cosmic_hours(_inputs(cosmic_cfp=200.0))[0]
    two = compute_cosmic_hours(_inputs(cosmic_cfp=400.0))[0]
    assert two == pytest.approx(2 * one)  # linear in CFP, no scale diseconomy
    hours, bd = compute_cosmic_hours(_inputs(cosmic_cfp=100.0, eaf_composite=1.0))
    assert bd["cosmic_cfp"] == 100.0 and bd["hours_per_cfp"] == HOURS_PER_CFP
    assert hours == pytest.approx(100.0 * HOURS_PER_CFP)  # eaf=1, modern_web stack=1, leverage=0


# ---------- build_phase_estimate method branch ----------


def _build(method: str) -> PhaseEstimate:
    inp = _inputs(function_points=300.0, sloc_estimate=14100.0, ai_reduction_pct=30.0)
    return build_phase_estimate(
        inp,
        effective_reduction=0.30,
        roster=RoleRoster.default(),
        rng=make_rng("t:development:2"),
        reduction_sampler=lambda _r: 0.30,
        sizing_method=method,
    )


def test_build_phase_estimate_labels_algorithm_by_method() -> None:
    assert _build("function_points").algorithm == "FUNCTION_POINTS"
    assert _build("cocomo").algorithm == "COCOMO_II"
    assert _build("cosmic_function_points").algorithm == "COSMIC_FFP"
    # Unknown method falls back to the default (COCOMO).
    assert _build("nonsense").algorithm == "COCOMO_II"


@pytest.mark.parametrize("method", ["function_points", "cosmic_function_points"])
def test_build_phase_estimate_preserves_ai_identity(method: str) -> None:
    est = _build(method)
    assert est.ai_assisted_hours.most_likely == pytest.approx(
        est.manual_only_hours.most_likely * (1 - 0.30)
    )


# ---------- method-agnostic ensemble fold ----------


def test_aggregate_dev_medians_both_drivers_and_keeps_fp() -> None:
    samples = [
        _inputs(sloc_estimate=10000.0, function_points=200.0, eaf_composite=1.0),
        _inputs(sloc_estimate=20000.0, function_points=400.0, eaf_composite=1.2),
        _inputs(sloc_estimate=30000.0, function_points=600.0, eaf_composite=1.4),
    ]
    folded = _aggregate_dev(samples)
    assert folded.sloc_estimate == pytest.approx(20000.0)
    # FP is kept (medianed), not nulled — so the FP method still has a size driver.
    assert folded.function_points == pytest.approx(400.0)
    # CFP is also medianed (derived from FP here: 200/400/600 × CFP_PER_FP) so COSMIC has a driver.
    assert folded.cosmic_cfp == pytest.approx(400.0 * CFP_PER_FP)
    assert folded.eaf_composite == pytest.approx(1.2)


# ---------- app_settings KV store ----------


@pytest_asyncio.fixture
async def in_memory_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    postgres_adapter._reset_for_tests()
    postgres_adapter._engine = engine
    postgres_adapter._sessionmaker = maker
    postgres_adapter._init_attempted = True
    try:
        yield maker
    finally:
        await engine.dispose()
        postgres_adapter._reset_for_tests()


@pytest.mark.asyncio
async def test_app_setting_round_trip(in_memory_db) -> None:
    assert await get_app_setting("development_sizing_method", "cocomo") == "cocomo"  # unset → default
    assert await set_app_setting("development_sizing_method", "function_points") is True
    assert await get_app_setting("development_sizing_method", "cocomo") == "function_points"
    # Upsert overwrites in place.
    await set_app_setting("development_sizing_method", "cocomo")
    assert await get_app_setting("development_sizing_method", "cocomo") == "cocomo"


@pytest.mark.asyncio
async def test_app_setting_defaults_when_postgres_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    postgres_adapter._reset_for_tests()
    monkeypatch.setattr(postgres_adapter, "get_sessionmaker", lambda: None)
    assert await get_app_setting("development_sizing_method", "cocomo") == "cocomo"
    assert await set_app_setting("development_sizing_method", "function_points") is False


# ---------- admin surface ----------


@pytest.mark.asyncio
async def test_dev_sizing_admin_default_shape() -> None:
    postgres_adapter._reset_for_tests()
    resp = await get_dev_sizing_method()
    assert resp.method == DEFAULT_DEV_SIZING_METHOD == "cocomo"
    assert resp.methods == ["cocomo", "function_points", "cosmic_function_points"]


@pytest.mark.asyncio
async def test_dev_sizing_admin_rejects_unknown_method() -> None:
    with pytest.raises(HTTPException) as exc:
        await update_dev_sizing_method(DevSizingUpdate(method="waterfall"))
    assert exc.value.status_code == 422


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def test_get_endpoint_returns_method() -> None:
    res = _client().get("/admin/development-sizing-method")
    assert res.status_code == 200
    body = res.json()
    assert body["method"] in body["methods"]
    assert "editable" in body


def test_put_endpoint_rejects_unknown_method() -> None:
    res = _client().put("/admin/development-sizing-method", json={"method": "waterfall"})
    assert res.status_code == 422

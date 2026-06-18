"""FP-based analysis-effort alternative for the Discovery twin + the discovery-sizing admin surface.

Covers: resolve_fp_discovery (explicit → UUCW fallback) / compute_fp_analysis_hours (linear, breakdown,
near-UCP calibration), the method branch in build_phase_estimate (algorithm label + ai = manual·(1−r)
identity), the method-aware _uncertain_fields_discovery (MC bands the right driver), and the
/admin/discovery-sizing-method GET/PUT endpoints. The app_settings KV round-trip itself is covered in
test_dev_sizing.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import db.postgres_adapter as postgres_adapter
from discovery_sizing_admin import (
    DiscoverySizingUpdate,
    get_discovery_sizing_method,
    update_discovery_sizing_method,
)
from models.project_schema import RoleRoster
from models.twin_outputs import PhaseEstimate
from orchestrator.montecarlo import make_rng
from orchestrator.nodes.discovery_analyst import (
    DEFAULT_DISCOVERY_SIZING_METHOD,
    FP_PER_UUCW,
    HOURS_PER_FP_ANALYSIS,
    DiscoveryUCPInputs,
    _uncertain_fields_discovery,
    _uucw,
    build_phase_estimate,
    compute_fp_analysis_hours,
    compute_ucp_hours,
    resolve_fp_discovery,
)


def _inputs(**kw: Any) -> DiscoveryUCPInputs:
    base: dict[str, Any] = dict(
        simple_use_cases=4,
        average_use_cases=6,
        complex_use_cases=2,
        simple_actors=1,
        average_actors=2,
        complex_actors=1,
        tfactor=20,
        efactor=20,
        stakeholder_group_count=2,
        decision_maker_accessibility="readily_available",
        alignment_difficulty="pre_aligned",
        confidence=0.7,
    )
    base.update(kw)
    return DiscoveryUCPInputs(**base)


# ---------- FP-analysis sizing math ----------


def test_resolve_fp_discovery_prefers_explicit_then_uucw_fallback() -> None:
    assert resolve_fp_discovery(_inputs(total_function_points=250.0)) == 250.0
    # No FP → derive from the unadjusted use-case weight via the ratio.
    inp = _inputs(total_function_points=None)
    assert resolve_fp_discovery(inp) == pytest.approx(_uucw(inp) * FP_PER_UUCW)


def test_compute_fp_analysis_hours_is_linear_and_breakdown() -> None:
    one = compute_fp_analysis_hours(_inputs(total_function_points=100.0))[0]
    two = compute_fp_analysis_hours(_inputs(total_function_points=200.0))[0]
    assert two == pytest.approx(2 * one)  # linear in FP
    hours, bd = compute_fp_analysis_hours(_inputs(total_function_points=100.0))
    assert bd["function_points"] == 100.0 and bd["hours_per_fp_analysis"] == HOURS_PER_FP_ANALYSIS
    # stakeholder_multiplier 1.0 for this fixture (2 groups, readily-available, pre-aligned).
    assert hours == pytest.approx(100.0 * HOURS_PER_FP_ANALYSIS)


def test_fp_analysis_is_calibrated_near_ucp() -> None:
    # The constants are tuned so a representative project lands near the UCP baseline.
    inp = _inputs()  # no explicit FP → UUCW-derived
    ucp_hours = compute_ucp_hours(inp)[0]
    fp_hours = compute_fp_analysis_hours(inp)[0]
    assert fp_hours == pytest.approx(ucp_hours, rel=0.2)


# ---------- build_phase_estimate method branch ----------


def _build(method: str, **kw: Any) -> PhaseEstimate:
    inp = _inputs(total_function_points=200.0, **kw)
    return build_phase_estimate(
        inp,
        effective_reduction=0.25,
        roster=RoleRoster.default(),
        rng=make_rng("t:discovery:2"),
        reduction_sampler=lambda _r: 0.25,
        sizing_method=method,
    )


def test_build_labels_algorithm_by_method() -> None:
    assert _build("ucp").algorithm == "UCP"
    assert _build("function_points").algorithm == "FP_ANALYSIS"
    # Unknown method falls back to the default (UCP).
    assert _build("nonsense").algorithm == "UCP"


def test_build_preserves_ai_identity_under_fp() -> None:
    est = _build("function_points")
    assert est.ai_assisted_hours.most_likely == pytest.approx(
        est.manual_only_hours.most_likely * (1 - 0.25)
    )


# ---------- method-aware MC field selection ----------


def test_uncertain_fields_bands_the_active_driver() -> None:
    inp = _inputs(total_function_points=200.0)
    assert set(_uncertain_fields_discovery(inp, "ucp")) == {"productivity_factor"}
    assert set(_uncertain_fields_discovery(inp, "function_points")) == {"total_function_points"}


def test_uncertain_fields_fp_band_is_non_degenerate_with_uucw_fallback() -> None:
    # Even when total_function_points is None (UUCW fallback), the band/point stay consistent.
    inp = _inputs(total_function_points=None, estimate_cov=0.3)
    band = _uncertain_fields_discovery(inp, "function_points")
    lo, mode, hi = band["total_function_points"]
    assert lo < mode < hi
    assert mode == pytest.approx(_uucw(inp) * FP_PER_UUCW)  # UUCW-derived FP


# ---------- admin surface ----------


@pytest.mark.asyncio
async def test_discovery_sizing_admin_default_shape() -> None:
    postgres_adapter._reset_for_tests()
    resp = await get_discovery_sizing_method()
    assert resp.method == DEFAULT_DISCOVERY_SIZING_METHOD == "ucp"
    assert resp.methods == ["ucp", "function_points"]


@pytest.mark.asyncio
async def test_discovery_sizing_admin_rejects_unknown_method() -> None:
    with pytest.raises(HTTPException) as exc:
        await update_discovery_sizing_method(DiscoverySizingUpdate(method="story_points"))
    assert exc.value.status_code == 422


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def test_get_endpoint_returns_method() -> None:
    res = _client().get("/admin/discovery-sizing-method")
    assert res.status_code == 200
    body = res.json()
    assert body["method"] in body["methods"]
    assert "editable" in body


def test_put_endpoint_rejects_unknown_method() -> None:
    res = _client().put("/admin/discovery-sizing-method", json={"method": "story_points"})
    assert res.status_code == 422

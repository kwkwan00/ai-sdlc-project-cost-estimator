"""Selectable QA sizing methods (Test Case Point + Capers-Jones defect-removal) + the qa-sizing
admin surface.

Covers: resolve_test_cases / compute_test_case_points (weighting + FP fallback) and
resolve_defect_density / compute_defect_removal_points (defect potential → removal points, baseline
calibration), both feeding the shared plan machinery; the method branch in build_phase_estimate
(algorithm label + ai identity), the method-aware _uncertain_fields_qa (MC bands the right driver),
and the /admin/qa-sizing-method GET/PUT endpoints. The app_settings KV round-trip itself is covered
in test_dev_sizing.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import db.postgres_adapter as postgres_adapter
from admin.qa_sizing_admin import (
    QaSizingUpdate,
    get_qa_sizing_method,
    update_qa_sizing_method,
)
from models.project_schema import RoleRoster
from models.twin_outputs import PhaseEstimate
from orchestrator.montecarlo import Range3, make_rng
from orchestrator.nodes.qa_testing_strategist import (
    DEFAULT_QA_SIZING_METHOD,
    DEFECTS_PER_FP,
    DRP_PER_DEFECT,
    TEST_CASES_PER_FP,
    TEST_REMOVAL_SHARE,
    TP_PER_WEIGHTED_CASE,
    QATPAInputs,
    _uncertain_fields_qa,
    build_phase_estimate,
    compute_defect_removal_points,
    compute_test_case_points,
    resolve_defect_density,
    resolve_test_cases,
)


def _inputs(**kw: Any) -> QATPAInputs:
    base: dict[str, Any] = dict(total_function_points=200.0, confidence=0.7)
    base.update(kw)
    return QATPAInputs(**base)


# ---------- TCPA sizing math ----------


def test_resolve_test_cases_prefers_explicit_then_fp_fallback() -> None:
    assert resolve_test_cases(_inputs(test_case_count=300.0)) == 300.0
    # No count → derive from FP via the ratio.
    assert resolve_test_cases(_inputs(test_case_count=None)) == pytest.approx(200.0 * TEST_CASES_PER_FP)


def test_compute_test_case_points_weights_by_checkpoints_and_is_linear() -> None:
    one = compute_test_case_points(_inputs(test_case_count=100.0, avg_checkpoints_per_case=5.0))[0]
    two = compute_test_case_points(_inputs(test_case_count=200.0, avg_checkpoints_per_case=5.0))[0]
    assert two == pytest.approx(2 * one)  # linear in case count
    # A nominal 5-checkpoint case has weight 1.0 → total_tcp = count × 0.5.
    assert one == pytest.approx(100.0 * 1.0 * TP_PER_WEIGHTED_CASE)
    # Double the checkpoints → double the weight → double the points.
    heavy = compute_test_case_points(_inputs(test_case_count=100.0, avg_checkpoints_per_case=10.0))[0]
    assert heavy == pytest.approx(2 * one)


def test_compute_test_case_points_breakdown_keys() -> None:
    _, bd = compute_test_case_points(_inputs(test_case_count=120.0, avg_checkpoints_per_case=5.0))
    assert bd["test_cases"] == 120.0
    assert bd["checkpoint_weight"] == pytest.approx(1.0)
    assert "total_tcp" in bd


# ---------- Capers-Jones defect-removal sizing math ----------


def test_resolve_defect_density_prefers_explicit_then_default() -> None:
    assert resolve_defect_density(_inputs(defect_density_per_fp=6.0)) == 6.0
    assert resolve_defect_density(_inputs(defect_density_per_fp=None)) == DEFECTS_PER_FP


def test_compute_defect_removal_points_is_linear_and_breakdown() -> None:
    one = compute_defect_removal_points(_inputs(total_function_points=200.0))[0]
    two = compute_defect_removal_points(_inputs(total_function_points=400.0))[0]
    assert two == pytest.approx(2 * one)  # linear in FP (defect potential = FP × density)
    drp, bd = compute_defect_removal_points(_inputs(total_function_points=200.0))
    assert bd["defect_density_per_fp"] == DEFECTS_PER_FP
    assert bd["defect_potential"] == pytest.approx(200.0 * DEFECTS_PER_FP)
    assert bd["test_removal_defects"] == pytest.approx(200.0 * DEFECTS_PER_FP * TEST_REMOVAL_SHARE)
    assert drp == pytest.approx(200.0 * DEFECTS_PER_FP * TEST_REMOVAL_SHARE * DRP_PER_DEFECT)


def test_defect_removal_nominal_lands_near_tpa_baseline() -> None:
    # Calibrated so a nominal project (FP=200, default density) ≈ the TPA total_tp (~119).
    drp = compute_defect_removal_points(_inputs(total_function_points=200.0))[0]
    assert drp == pytest.approx(120.0)


# ---------- build_phase_estimate method branch ----------


def _build(method: str, **kw: Any) -> PhaseEstimate:
    params: dict[str, Any] = dict(test_case_count=300.0, ai_reduction_pct=20.0)
    params.update(kw)
    inp = _inputs(**params)
    return build_phase_estimate(
        inp,
        effective_reduction=0.20,
        roster=RoleRoster.default(),
        rng=make_rng("t:qa_testing:2"),
        reduction_sampler=lambda _r: 0.20,
        sizing_method=method,
    )


def test_build_labels_algorithm_by_method_and_plan() -> None:
    assert _build("test_case_point").algorithm == "TCPA_Plan_A"
    assert _build("tpa").algorithm == "TPA_Plan_A"
    assert _build("defect_removal").algorithm == "DEFECT_Plan_A"
    # Unknown method falls back to the default (TPA).
    assert _build("nonsense").algorithm == "TPA_Plan_A"


@pytest.mark.parametrize("method", ["test_case_point", "defect_removal"])
def test_build_preserves_ai_identity(method: str) -> None:
    est = _build(method)
    assert est.ai_assisted_hours.most_likely == pytest.approx(
        est.manual_only_hours.most_likely * (1 - 0.20)
    )


def test_tpa_and_tcpa_can_diverge() -> None:
    # Many heavy test cases vs a modest FP count → TCPA > TPA for the same inputs.
    tpa = _build("tpa", total_function_points=100.0).manual_only_hours.most_likely
    tcpa = _build("test_case_point", total_function_points=100.0, test_case_count=600.0,
                  avg_checkpoints_per_case=10.0).manual_only_hours.most_likely
    assert tcpa > tpa


# ---------- method-aware MC field selection ----------


def test_uncertain_fields_bands_the_active_driver() -> None:
    inp = _inputs(test_case_count=300.0)
    assert set(_uncertain_fields_qa(inp, "tpa")) == {"total_function_points"}
    assert set(_uncertain_fields_qa(inp, "test_case_point")) == {"test_case_count"}
    # Defect-removal scales off FP, so it bands total_function_points like TPA.
    assert set(_uncertain_fields_qa(inp, "defect_removal")) == {"total_function_points"}


def test_uncertain_fields_tcpa_converts_fp_range_to_cases() -> None:
    # Only an fp_range given (no test_case_range) → it's converted to test-case units.
    inp = _inputs(test_case_count=None, fp_range=Range3(low=100.0, high=300.0))
    band = _uncertain_fields_qa(inp, "test_case_point")
    lo, _mode, hi = band["test_case_count"]
    assert lo == pytest.approx(100.0 * TEST_CASES_PER_FP)
    assert hi == pytest.approx(300.0 * TEST_CASES_PER_FP)


def test_uncertain_fields_tcpa_band_is_non_degenerate_with_fp_only() -> None:
    # Even when test_case_count is None (FP fallback), the band/point stay consistent (non-zero width).
    inp = _inputs(test_case_count=None, estimate_cov=0.3)
    band = _uncertain_fields_qa(inp, "test_case_point")
    lo, mode, hi = band["test_case_count"]
    assert lo < mode < hi
    assert mode == pytest.approx(200.0 * TEST_CASES_PER_FP)  # FP-derived case count


# ---------- admin surface ----------


@pytest.mark.asyncio
async def test_qa_sizing_admin_default_shape() -> None:
    postgres_adapter._reset_for_tests()
    resp = await get_qa_sizing_method()
    assert resp.method == DEFAULT_QA_SIZING_METHOD == "tpa"
    assert resp.methods == ["tpa", "test_case_point", "defect_removal"]


@pytest.mark.asyncio
async def test_qa_sizing_admin_rejects_unknown_method() -> None:
    with pytest.raises(HTTPException) as exc:
        await update_qa_sizing_method(QaSizingUpdate(method="exploratory"))
    assert exc.value.status_code == 422


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def test_get_endpoint_returns_method() -> None:
    res = _client().get("/admin/qa-sizing-method")
    assert res.status_code == 200
    body = res.json()
    assert body["method"] in body["methods"]
    assert "editable" in body


def test_put_endpoint_rejects_unknown_method() -> None:
    res = _client().put("/admin/qa-sizing-method", json={"method": "exploratory"})
    assert res.status_code == 422

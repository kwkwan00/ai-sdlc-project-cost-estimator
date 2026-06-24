"""Offline unit tests for the Monte Carlo uncertainty-propagation module.

Pure stdlib + Pydantic; needs no ANTHROPIC_API_KEY and hits no network. All
statistical assertions pin a seed (via ``make_rng``) and use modest draw counts
(500-2000) with honest tolerances so the suite stays fast and deterministic.

Reference identities (from ``orchestrator/montecarlo.py``):
  - Beta-PERT mean = (low + 4*mode + high) / 6
  - manual_draw = base + risk ; ai_draw = base*(1 - r) + risk
  - manual.point = compute_fn(point_inputs)[0] ; ai.point = manual.point*(1 - eff_point)
  - result_to_hour_range: most_likely=point (deterministic mode); optimistic/pessimistic = P10/P90 expanded to bracket the point
"""

from __future__ import annotations

import statistics

import pytest
from pydantic import BaseModel, ValidationError

from models.project_schema import AiToolingLevel, CodebaseContext, RoleRoster
from models.twin_outputs import HourRange, Phase
from orchestrator.ai_acceleration import ReductionContext, effective_ai_reduction
from orchestrator.montecarlo import (
    DEFAULT_DRAWS,
    MCResult,
    Range3,
    _confidence_cov,
    make_rng,
    propagate_phase,
    resolve_size_band,
    result_to_hour_range,
    sample_pert,
    sample_risks,
)
from orchestrator.nodes._twin_base import make_reduction_sampler


def _pert_mean(low: float, mode: float, high: float) -> float:
    """Closed-form Beta-PERT (lam=4) expected value."""
    return (low + 4.0 * mode + high) / 6.0


# ---------------------------------------------------------------------------
# 1. make_rng determinism
# ---------------------------------------------------------------------------


def test_make_rng_same_seed_identical_pert_sequence() -> None:
    rng_a = make_rng("est:discovery:1")
    rng_b = make_rng("est:discovery:1")
    seq_a = [sample_pert(0.0, 5.0, 10.0, rng_a) for _ in range(50)]
    seq_b = [sample_pert(0.0, 5.0, 10.0, rng_b) for _ in range(50)]
    assert seq_a == seq_b


def test_make_rng_different_seeds_differ() -> None:
    rng_a = make_rng("est:discovery:1")
    rng_b = make_rng("est:discovery:2")
    seq_a = [sample_pert(0.0, 5.0, 10.0, rng_a) for _ in range(50)]
    seq_b = [sample_pert(0.0, 5.0, 10.0, rng_b) for _ in range(50)]
    assert seq_a != seq_b


def test_make_rng_returns_independent_objects() -> None:
    # Distinct RNG objects, so consuming one does not advance the other.
    rng_a = make_rng("seed-x")
    rng_b = make_rng("seed-x")
    [sample_pert(0.0, 1.0, 2.0, rng_a) for _ in range(10)]
    # rng_b is untouched; its first draws still match a fresh rng on the same seed.
    rng_c = make_rng("seed-x")
    assert sample_pert(0.0, 1.0, 2.0, rng_b) == sample_pert(0.0, 1.0, 2.0, rng_c)


# ---------------------------------------------------------------------------
# 2. sample_pert
# ---------------------------------------------------------------------------


def test_sample_pert_within_bounds() -> None:
    rng = make_rng("pert-bounds")
    low, mode, high = 10.0, 25.0, 100.0
    for _ in range(2000):
        x = sample_pert(low, mode, high, rng)
        assert low <= x <= high


def test_sample_pert_degenerate_returns_value() -> None:
    rng = make_rng("pert-degenerate")
    for _ in range(20):
        assert sample_pert(42.0, 42.0, 42.0, rng) == 42.0


def test_sample_pert_zero_span_returns_mode() -> None:
    # low == high (span == 0) short-circuits to the mode without touching the RNG.
    rng = make_rng("pert-zero-span")
    assert sample_pert(7.0, 7.0, 7.0, rng) == 7.0


def test_sample_pert_mean_matches_beta_pert() -> None:
    rng = make_rng("pert-mean")
    low, mode, high = 10.0, 20.0, 60.0
    draws = [sample_pert(low, mode, high, rng) for _ in range(2000)]
    sample_mean = statistics.fmean(draws)
    expected = _pert_mean(low, mode, high)  # (10 + 80 + 60)/6 = 25.0
    # Loose tolerance: 2000 draws on a ~CoV 0.3 driver -> SE well under this.
    assert expected == pytest.approx(25.0)
    assert sample_mean == pytest.approx(expected, rel=0.05)


def test_sample_pert_right_skew_when_mode_left_of_center() -> None:
    # Mode well left of the midpoint -> right-skewed -> median < mean.
    rng = make_rng("pert-skew")
    low, mode, high = 0.0, 10.0, 100.0
    draws = [sample_pert(low, mode, high, rng) for _ in range(2000)]
    median = statistics.median(draws)
    mean = statistics.fmean(draws)
    assert median < mean


# ---------------------------------------------------------------------------
# 2b. AI-effectiveness prior shape (Option 1 — left-skewed / downside-weighted)
# ---------------------------------------------------------------------------


def test_reduction_prior_is_downside_weighted() -> None:
    """The reshaped AI-effectiveness prior leans toward the pessimistic (lower-reduction) side, so
    the EXPECTED realized reduction sits BELOW the deterministic point. The point (most_likely) is
    computed separately and unchanged; only the band leans down — and it still brackets the point."""
    ctx = ReductionContext(
        phase=Phase.DEVELOPMENT,
        codebase=CodebaseContext.GREENFIELD,
        tooling=AiToolingLevel.AGENTIC,  # dev/agentic has a real band → sampler isn't constant 0
        roster=RoleRoster.default(),
        regulated=False,
        bands=None,
    )
    point = 0.55
    r_point = effective_ai_reduction(proposed_reduction=point, **ctx.reduction_kwargs())
    sampler = make_reduction_sampler(ctx=ctx, proposed_point=point, reduction_range=None)
    rng = make_rng("reduction-skew")
    draws = [sampler(rng) for _ in range(4000)]
    assert statistics.fmean(draws) < r_point         # downside-weighted (heavier low tail)
    assert min(draws) <= r_point <= max(draws)        # band still brackets the deterministic point


# ---------------------------------------------------------------------------
# 3. sample_risks
# ---------------------------------------------------------------------------


def test_sample_risks_probability_zero_always_zero() -> None:
    rng = make_rng("risk-zero")
    for _ in range(500):
        assert sample_risks([(0.0, 100.0, 300.0)], rng) == 0.0


def test_sample_risks_certain_within_bounds() -> None:
    rng = make_rng("risk-certain")
    for _ in range(1000):
        total = sample_risks([(1.0, 100.0, 300.0)], rng)
        assert 100.0 <= total <= 300.0


def test_sample_risks_multiple_certain_risks_sum() -> None:
    rng = make_rng("risk-sum")
    specs = [(1.0, 100.0, 300.0), (1.0, 50.0, 150.0)]
    for _ in range(1000):
        total = sample_risks(specs, rng)
        # Both certain risks fire every draw; total is the sum of the two PERT draws.
        assert 150.0 <= total <= 450.0


def test_sample_risks_no_specs_returns_zero() -> None:
    rng = make_rng("risk-empty")
    assert sample_risks([], rng) == 0.0


def test_sample_risks_expected_total_matches_sum_p_times_pert_mean() -> None:
    rng = make_rng("risk-expectation")
    # Mix of probabilities and bands. Expected per-draw total = Σ p_k * pert_mean_k,
    # where pert_mean uses midpoint as the mode -> mean == band midpoint.
    specs: list[tuple[float, float, float]] = [
        (0.5, 100.0, 300.0),  # mid 200, contributes 0.5 * 200 = 100
        (0.2, 50.0, 150.0),   # mid 100, contributes 0.2 * 100 = 20
        (1.0, 40.0, 80.0),    # mid 60,  contributes 1.0 * 60  = 60
    ]
    expected = sum(p * _pert_mean(lo, (lo + hi) / 2.0, hi) for p, lo, hi in specs)
    assert expected == pytest.approx(180.0)
    draws = [sample_risks(specs, rng) for _ in range(2000)]
    assert statistics.fmean(draws) == pytest.approx(expected, rel=0.10)


# ---------------------------------------------------------------------------
# 4. Range3
# ---------------------------------------------------------------------------


def test_range3_swaps_low_high() -> None:
    r = Range3(low=10.0, high=2.0)
    assert r.low == 2.0
    assert r.high == 10.0


def test_range3_keeps_ordered_input() -> None:
    r = Range3(low=2.0, high=10.0)
    assert (r.low, r.high) == (2.0, 10.0)


def test_range3_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Range3(low=1.0, high=2.0, mode=1.5)  # type: ignore[call-arg]


def test_range3_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        Range3(low=-1.0, high=2.0)


# ---------------------------------------------------------------------------
# 5. resolve_size_band + _confidence_cov
# ---------------------------------------------------------------------------


def test_resolve_size_band_explicit_used_mode_clamped_to_point() -> None:
    band = resolve_size_band(
        point_value=50.0,
        explicit=Range3(low=30.0, high=90.0),
        estimate_cov=None,
        confidence=0.5,
    )
    assert band is not None
    low, mode, high = band
    assert low == 30.0
    assert high == 90.0
    assert mode == 50.0  # point sits inside [low, high]


def test_resolve_size_band_explicit_mode_clamps_when_point_outside() -> None:
    # point below the explicit low -> low coerced down to point, mode == point.
    band = resolve_size_band(
        point_value=20.0,
        explicit=Range3(low=30.0, high=90.0),
        estimate_cov=None,
        confidence=0.5,
    )
    assert band is not None
    low, mode, high = band
    assert low == 20.0  # max(0, min(30, 20))
    assert high == 90.0
    assert mode == 20.0


def test_resolve_size_band_estimate_cov_widens_with_cov() -> None:
    narrow = resolve_size_band(
        point_value=100.0, explicit=None, estimate_cov=0.1, confidence=0.5
    )
    wide = resolve_size_band(
        point_value=100.0, explicit=None, estimate_cov=0.4, confidence=0.5
    )
    assert narrow is not None and wide is not None
    # Higher cov -> lower low and higher high.
    assert wide[0] < narrow[0]
    assert wide[2] > narrow[2]
    # Sanity: explicit cov formula low = point*(1 - 1.5*cov), high = point*(1 + 2*cov).
    assert narrow[0] == pytest.approx(100.0 * (1 - 1.5 * 0.1))
    assert narrow[2] == pytest.approx(100.0 * (1 + 2.0 * 0.1))
    assert narrow[1] == 100.0  # mode stays at point


def test_resolve_size_band_confidence_fallback_when_both_none() -> None:
    # Both explicit and estimate_cov None -> cov comes from _confidence_cov.
    point = 100.0
    confidence = 0.2
    band = resolve_size_band(
        point_value=point, explicit=None, estimate_cov=None, confidence=confidence
    )
    assert band is not None
    cov = _confidence_cov(confidence)
    assert band[0] == pytest.approx(point * (1 - 1.5 * cov))
    assert band[2] == pytest.approx(point * (1 + 2.0 * cov))


def test_resolve_size_band_clamps_to_bounds() -> None:
    band = resolve_size_band(
        point_value=100.0,
        explicit=Range3(low=10.0, high=500.0),
        estimate_cov=None,
        confidence=0.5,
        lo_bound=20.0,
        hi_bound=300.0,
    )
    assert band is not None
    low, mode, high = band
    assert low == 20.0  # raised to lo_bound
    assert high == 300.0  # lowered to hi_bound
    assert mode == 100.0


def test_resolve_size_band_none_when_point_non_positive() -> None:
    assert (
        resolve_size_band(
            point_value=0.0, explicit=Range3(low=1.0, high=2.0),
            estimate_cov=0.2, confidence=0.5,
        )
        is None
    )
    assert (
        resolve_size_band(
            point_value=-5.0, explicit=None, estimate_cov=None, confidence=0.5
        )
        is None
    )


def test_confidence_cov_monotonic_decreasing_and_clamped() -> None:
    covs = [_confidence_cov(c) for c in (0.0, 0.25, 0.5, 0.75, 1.0)]
    # Strictly non-increasing as confidence rises.
    assert all(covs[i] >= covs[i + 1] for i in range(len(covs) - 1))
    # Clamped to [0.10, 0.45] across the full and out-of-range domain.
    for c in (-1.0, 0.0, 0.5, 1.0, 2.0):
        v = _confidence_cov(c)
        assert 0.10 <= v <= 0.45
    assert _confidence_cov(1.0) == pytest.approx(0.12)  # 0.12 + 0 -> clamps stay 0.12
    assert _confidence_cov(0.0) == pytest.approx(0.45)  # 0.12 + 0.35 = 0.47 -> 0.45


# ---------------------------------------------------------------------------
# 6. propagate_phase
# ---------------------------------------------------------------------------


class _In(BaseModel):
    v: float


def _linear(x: _In) -> tuple[float, dict]:
    return (x.v * 10.0, {})


def _nonlinear(x: _In) -> tuple[float, dict]:
    return (x.v**1.3, {})


def test_propagate_phase_linear_percentiles_scale_with_driver() -> None:
    seed = "lin:driver"
    # Sample the driver itself with the SAME seed + draw count, then compare.
    low, mode, high = 8.0, 10.0, 14.0
    n = 2000
    driver_rng = make_rng(seed)
    driver = sorted(sample_pert(low, mode, high, driver_rng) for _ in range(n))

    manual, _ai = propagate_phase(
        _In(v=mode),
        _linear,
        size_fields={"v": (low, mode, high)},
        reduction_sampler=lambda rng: 0.0,  # ignores rng -> stream untouched
        risk_specs=[],
        eff_point=0.0,
        n_draws=n,
        rng=make_rng(seed),
    )

    def driver_pct(q: float) -> float:
        idx = q * (len(driver) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(driver) - 1)
        return driver[lo] + (idx - lo) * (driver[hi] - driver[lo])

    # compute_fn multiplies by 10, reduction is 0, no risk -> manual == 10 * driver,
    # draw-for-draw and therefore percentile-for-percentile.
    assert manual.p10 == pytest.approx(10.0 * driver_pct(0.10), rel=1e-9)
    assert manual.p50 == pytest.approx(10.0 * driver_pct(0.50), rel=1e-9)
    assert manual.p90 == pytest.approx(10.0 * driver_pct(0.90), rel=1e-9)


def test_propagate_phase_point_anchors() -> None:
    eff = 0.4
    manual, ai = propagate_phase(
        _In(v=10.0),
        _linear,
        size_fields={"v": (8.0, 10.0, 14.0)},
        reduction_sampler=lambda rng: 0.2,
        risk_specs=[],
        eff_point=eff,
        n_draws=500,
        rng=make_rng("anchors"),
    )
    assert manual.point == 100.0  # compute_fn(point_inputs)[0] == 10 * 10
    assert ai.point == pytest.approx(100.0 * (1 - eff))


def test_propagate_phase_constant_reduction_ai_is_manual_times_complement() -> None:
    # reduction is a constant 0.3 and no risks -> every ai draw == manual draw * 0.7.
    manual, ai = propagate_phase(
        _In(v=10.0),
        _linear,
        size_fields={"v": (8.0, 10.0, 14.0)},
        reduction_sampler=lambda rng: 0.3,
        risk_specs=[],
        eff_point=0.3,
        n_draws=2000,
        rng=make_rng("ai-complement"),
    )
    assert ai.p50 == pytest.approx(manual.p50 * 0.7, rel=1e-9)
    assert ai.p10 == pytest.approx(manual.p10 * 0.7, rel=1e-9)
    assert ai.p90 == pytest.approx(manual.p90 * 0.7, rel=1e-9)
    assert ai.mean == pytest.approx(manual.mean * 0.7, rel=1e-9)


def test_propagate_phase_risks_raise_mean_above_point_mode_near_point() -> None:
    # No size uncertainty + no reduction noise, but an infrequent risk -> the mean
    # is pulled above the deterministic point while the modal (no-risk) draw stays
    # at point, so P10 stays at the point.
    manual, _ai = propagate_phase(
        _In(v=10.0),
        _linear,
        size_fields={},  # no size uncertainty
        reduction_sampler=lambda rng: 0.0,
        risk_specs=[(0.2, 200.0, 400.0)],
        eff_point=0.0,
        n_draws=2000,
        rng=make_rng("risk-mean"),
    )
    assert manual.point == 100.0
    assert manual.mean > manual.point  # risk adds positive expected hours
    # The no-risk draw (the mode) equals the point; with a 20% risk the 10th
    # percentile is still a no-risk draw, so P10 == point.
    assert manual.p10 == pytest.approx(100.0)


def test_propagate_phase_fully_degenerate() -> None:
    # No size band, no risk, constant reduction -> every draw identical -> degenerate.
    manual, ai = propagate_phase(
        _In(v=10.0),
        _linear,
        size_fields={},
        reduction_sampler=lambda rng: 0.25,
        risk_specs=[],
        eff_point=0.25,
        n_draws=500,
        rng=make_rng("degenerate"),
    )
    assert manual.degenerate is True
    assert manual.p10 == manual.p90 == manual.point == 100.0
    assert manual.std == 0.0
    assert ai.degenerate is True
    assert ai.p10 == ai.p90 == ai.point
    assert ai.point == pytest.approx(75.0)


def test_propagate_phase_nonlinear_right_skew() -> None:
    # A convex compute_fn (v**1.3) on a symmetric-ish driver band produces a
    # right-skewed output: the upper tail (P90-P50) is fatter than the lower
    # tail (P50-P10).
    manual, _ai = propagate_phase(
        _In(v=50.0),
        _nonlinear,
        size_fields={"v": (20.0, 50.0, 90.0)},
        reduction_sampler=lambda rng: 0.0,
        risk_specs=[],
        eff_point=0.0,
        n_draws=2000,
        rng=make_rng("nonlinear-skew"),
    )
    upper = manual.p90 - manual.p50
    lower = manual.p50 - manual.p10
    assert upper > lower
    assert manual.degenerate is False


# ---------------------------------------------------------------------------
# 7. result_to_hour_range
# ---------------------------------------------------------------------------


def _mc(
    *,
    point: float,
    p10: float,
    p50: float,
    p90: float,
    mean: float,
    std: float,
    degenerate: bool,
) -> MCResult:
    return MCResult(
        point=point,
        p10=p10,
        p50=p50,
        p90=p90,
        mean=mean,
        std=std,
        n=100,
        degenerate=degenerate,
        percentiles={"p10": p10, "p50": p50, "p90": p90},
    )


def test_result_to_hour_range_ordering_and_population() -> None:
    mc = _mc(point=100.0, p10=80.0, p50=105.0, p90=160.0, mean=110.0, std=25.0, degenerate=False)
    hr = result_to_hour_range(mc)
    assert isinstance(hr, HourRange)
    assert hr.optimistic <= hr.most_likely <= hr.pessimistic
    assert hr.optimistic == 80.0
    assert hr.most_likely == 100.0  # point already inside [p10, p90]
    assert hr.pessimistic == 160.0
    assert hr.std == 25.0
    assert hr.mean == 110.0
    assert hr.percentiles == {"p10": 80.0, "p50": 105.0, "p90": 160.0}


def test_result_to_hour_range_point_above_p90_expands_pessimistic() -> None:
    # most_likely is ALWAYS the deterministic point; pessimistic expands to bracket it.
    mc = _mc(point=500.0, p10=80.0, p50=105.0, p90=160.0, mean=110.0, std=25.0, degenerate=False)
    hr = result_to_hour_range(mc)
    assert hr.most_likely == 500.0  # deterministic mode preserved
    assert hr.optimistic == 80.0
    assert hr.pessimistic == 500.0  # max(point, p90)
    assert hr.optimistic <= hr.most_likely <= hr.pessimistic


def test_result_to_hour_range_point_below_p10_collapses_optimistic() -> None:
    # When a high-prob risk pushes the mode below P10, optimistic collapses to the point.
    mc = _mc(point=10.0, p10=80.0, p50=105.0, p90=160.0, mean=110.0, std=25.0, degenerate=False)
    hr = result_to_hour_range(mc)
    assert hr.most_likely == 10.0  # deterministic mode preserved
    assert hr.optimistic == 10.0  # min(point, p10)
    assert hr.pessimistic == 160.0
    assert hr.optimistic <= hr.most_likely <= hr.pessimistic


def test_result_to_hour_range_degenerate_collapses_to_point() -> None:
    mc = _mc(point=100.0, p10=100.0, p50=100.0, p90=100.0, mean=100.0, std=0.0, degenerate=True)
    hr = result_to_hour_range(mc)
    assert hr.optimistic == hr.most_likely == hr.pessimistic == 100.0


# ---------------------------------------------------------------------------
# 8. Whole-pipeline determinism
# ---------------------------------------------------------------------------


def test_propagate_phase_pipeline_deterministic() -> None:
    kwargs = dict(
        size_fields={"v": (8.0, 10.0, 14.0)},
        risk_specs=[(0.3, 50.0, 150.0)],
        eff_point=0.35,
        n_draws=1000,
    )

    def run() -> tuple[MCResult, MCResult]:
        return propagate_phase(
            _In(v=10.0),
            _linear,
            reduction_sampler=lambda rng: 0.1 + rng.random() * 0.2,  # consumes rng
            rng=make_rng("pipeline:same"),
            **kwargs,  # type: ignore[arg-type]
        )

    manual_a, ai_a = run()
    manual_b, ai_b = run()
    assert manual_a == manual_b
    assert ai_a == ai_b


def test_propagate_phase_pipeline_differs_on_different_seed() -> None:
    kwargs = dict(
        size_fields={"v": (8.0, 10.0, 14.0)},
        reduction_sampler=lambda rng: rng.random() * 0.3,
        risk_specs=[(0.3, 50.0, 150.0)],
        eff_point=0.35,
        n_draws=1000,
    )
    manual_a, _ = propagate_phase(_In(v=10.0), _linear, rng=make_rng("seed-1"), **kwargs)  # type: ignore[arg-type]
    manual_b, _ = propagate_phase(_In(v=10.0), _linear, rng=make_rng("seed-2"), **kwargs)  # type: ignore[arg-type]
    assert manual_a != manual_b


def test_default_draws_is_positive_int() -> None:
    # Smoke check on the env-overridable constant the production call sites use.
    assert isinstance(DEFAULT_DRAWS, int)
    assert DEFAULT_DRAWS > 0

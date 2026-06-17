"""Unit tests for the team-scaling model (orchestrator/staffing.py): Brooks coordination
overhead + diminishing-returns throughput. Pure, offline, deterministic."""

from __future__ import annotations

from orchestrator.staffing import (
    DEFAULT_STAFFING_COEFFS,
    coordination_overhead,
    optimal_team_size,
    staffing_efficiency,
    team_throughput,
)

WPW = 32  # work hours per week


# --- Brooks coordination overhead -------------------------------------------


def test_overhead_zero_at_or_below_free_team_size() -> None:
    free = int(DEFAULT_STAFFING_COEFFS["free_team_size"])
    assert coordination_overhead(1) == 0.0
    assert coordination_overhead(free) == 0.0
    assert coordination_overhead(free + 1) > 0.0


def test_overhead_monotonic_and_capped() -> None:
    cap = DEFAULT_STAFFING_COEFFS["overhead_cap"]
    prev = -1.0
    for n in range(1, 60):
        o = coordination_overhead(n)
        assert o >= prev - 1e-12  # non-decreasing
        assert 0.0 <= o <= cap
        prev = o
    assert coordination_overhead(500) == cap  # saturates at the cap


def test_overhead_honors_override_coeffs() -> None:
    assert coordination_overhead(10, {"link_cost": 0.2}) > coordination_overhead(10)


# --- diminishing-returns throughput + efficiency ----------------------------


def test_throughput_linear_when_beta_one_and_no_overhead() -> None:
    coeffs = {"diminishing_returns_exponent": 1.0, "free_team_size": 1000.0}
    for n in (1, 2, 5, 10):
        assert team_throughput(n, coeffs) == n  # n**1 · (1 − 0)


def test_throughput_is_concave_under_diminishing_returns() -> None:
    # Below the free team size (no coordination overhead) only β bites: doubling the team
    # less-than-doubles the throughput.
    coeffs = {"free_team_size": 1000.0}  # default β = 0.9
    assert team_throughput(2, coeffs) < 2 * team_throughput(1, coeffs)
    assert team_throughput(8, coeffs) < 2 * team_throughput(4, coeffs)


def test_throughput_peaks_then_falls() -> None:
    # With unbounded coordination loss in the throughput curve, a big-enough team is net
    # WORSE than a smaller one — the Brooks turning point.
    peak = max(range(1, 60), key=team_throughput)
    assert 1 < peak < 59
    assert team_throughput(peak) > team_throughput(peak + 5)


def test_efficiency_is_one_for_solo_and_decreasing() -> None:
    assert staffing_efficiency(1) == 1.0
    assert staffing_efficiency(2) < 1.0
    assert staffing_efficiency(10) < staffing_efficiency(5)


# --- optimal team size ------------------------------------------------------


def test_optimal_team_size_scales_with_project_size() -> None:
    tiny = optimal_team_size(40, WPW)  # ~1 week of work → a single dev
    big = optimal_team_size(50_000, WPW)  # lots of work → the coordination sweet spot
    assert tiny == 1
    assert big > tiny
    # It maximises throughput within the search bound (so duration doesn't fall past it).
    assert team_throughput(big) >= team_throughput(big + 1)


def test_optimal_team_size_degenerate_inputs() -> None:
    assert optimal_team_size(0, WPW) == 1
    assert optimal_team_size(1000, 0) == 1


def test_model_is_pure_and_deterministic() -> None:
    assert coordination_overhead(12) == coordination_overhead(12)
    assert optimal_team_size(9000, WPW) == optimal_team_size(9000, WPW)

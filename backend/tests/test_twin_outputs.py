"""Model-invariant tests for twin output schemas.

Covers the HourRange PERT-ordering coercion (graceful repair of malformed LLM
ranges, no hard raise) and the backward-compatible consistency_warnings field on
DualScenarioEstimate.
"""

from __future__ import annotations

from models.twin_outputs import (
    DualScenarioEstimate,
    HourRange,
    Phase,
    PhaseEstimate,
)


def test_hour_range_reorders_when_most_likely_is_smallest() -> None:
    # Spec: optimistic = min(o, m, p), pessimistic = max(o, m, p), then clamp
    # most_likely into the new range. Here min=5 (the most_likely value) so it
    # becomes the optimistic bound; most_likely is then >= optimistic as required.
    hr = HourRange(optimistic=10, most_likely=5, pessimistic=20)
    assert hr.optimistic == 5
    assert hr.pessimistic == 20
    assert hr.most_likely == 5
    assert hr.optimistic <= hr.most_likely <= hr.pessimistic


def test_hour_range_swaps_inverted_optimistic_pessimistic() -> None:
    # optimistic > pessimistic: swap so optimistic is the min and pessimistic the max.
    hr = HourRange(optimistic=30, most_likely=20, pessimistic=10)
    assert hr.optimistic == 10
    assert hr.pessimistic == 30
    assert hr.most_likely == 20  # already inside range, untouched


def test_hour_range_reorders_when_most_likely_is_largest() -> None:
    # most_likely (50) is the max, so it becomes the pessimistic bound and is then
    # clamped to == pessimistic. optimistic stays the true minimum (5).
    hr = HourRange(optimistic=5, most_likely=50, pessimistic=20)
    assert hr.optimistic == 5
    assert hr.pessimistic == 50
    assert hr.most_likely == 50
    assert hr.optimistic <= hr.most_likely <= hr.pessimistic


def test_hour_range_valid_passes_through_unchanged() -> None:
    hr = HourRange(optimistic=10, most_likely=15, pessimistic=25)
    assert hr.optimistic == 10
    assert hr.most_likely == 15
    assert hr.pessimistic == 25
    assert hr.pert_mean == (10 + 4 * 15 + 25) / 6


def _dual_scenario(**overrides: object) -> DualScenarioEstimate:
    kwargs: dict[str, object] = {
        "total_ai_assisted_hours": HourRange(
            optimistic=100, most_likely=120, pessimistic=160
        ),
        "total_manual_only_hours": HourRange(
            optimistic=140, most_likely=170, pessimistic=220
        ),
        "ai_hours_saved_pert": 50.0,
        "phases": [],
        "confidence": 0.7,
        "duration_weeks_low": 4.0,
        "duration_weeks_high": 8.0,
    }
    kwargs.update(overrides)
    return DualScenarioEstimate(**kwargs)  # type: ignore[arg-type]


def test_dual_scenario_consistency_warnings_defaults_empty() -> None:
    est = _dual_scenario()
    assert est.consistency_warnings == []


def test_dual_scenario_consistency_warnings_accepts_populated_list() -> None:
    warnings = ["dev hours exceed manual baseline", "qa under-allocated"]
    est = _dual_scenario(consistency_warnings=warnings)
    assert est.consistency_warnings == warnings


def test_phase_estimate_coerces_nested_malformed_hour_range() -> None:
    # Ensure coercion fires when an HourRange is built via a nested model too.
    est = PhaseEstimate(
        phase=Phase.DEVELOPMENT,
        twin_name="development_architect",
        algorithm="COCOMO II",
        ai_assisted_hours=HourRange(optimistic=300, most_likely=200, pessimistic=100),
        manual_only_hours=HourRange(optimistic=120, most_likely=140, pessimistic=180),
        confidence=0.6,
    )
    assert est.ai_assisted_hours.optimistic == 100
    assert est.ai_assisted_hours.pessimistic == 300
    assert est.ai_assisted_hours.most_likely == 200

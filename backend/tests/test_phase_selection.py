"""Unit tests for phase-subset selection — the twin guard + request validation.

The end-to-end graph behavior (a subset runs only the selected twins and synthesizes only those
phases; the single-phase degenerate case stays finite) lives in test_graph.py, which reuses the
offline graph fixture. These cover the small, pure pieces in isolation.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from models.estimation_state import EstimationState
from models.project_schema import CreateEstimateRequest
from models.twin_outputs import HourRange, Phase, PhaseEstimate
from orchestrator.nodes._twin_base import _phase_selected
from orchestrator.nodes.synthesize_estimate import _combine_range

_RAW = "Build an internal tool for the operations team."  # ≥10 chars for the request validator


# --- the guard truth table ---------------------------------------------------


def test_phase_selected_absent_or_empty_means_all() -> None:
    # No key (omitted at graph entry) or an empty list ⇒ every phase runs (back-compat with
    # existing callers, the smoke test, and the WBS flow, none of which set selected_phases).
    assert _phase_selected({}, Phase.DEVELOPMENT) is True
    assert _phase_selected({"selected_phases": []}, Phase.DISCOVERY) is True


def test_phase_selected_respects_membership() -> None:
    state: EstimationState = {"selected_phases": [Phase.DEVELOPMENT, Phase.QA_TESTING]}
    assert _phase_selected(state, Phase.DEVELOPMENT) is True
    assert _phase_selected(state, Phase.QA_TESTING) is True
    assert _phase_selected(state, Phase.DISCOVERY) is False
    assert _phase_selected(state, Phase.UX_DESIGN) is False


# --- request model validation ------------------------------------------------


def test_request_omitting_selection_defaults_to_none() -> None:
    req = CreateEstimateRequest(raw_input=_RAW)
    assert req.selected_phases is None  # ⇒ all six downstream


def test_request_dedupes_selected_phases_order_preserving() -> None:
    req = CreateEstimateRequest(
        raw_input=_RAW,
        selected_phases=[Phase.QA_TESTING, Phase.DEVELOPMENT, Phase.QA_TESTING],
    )
    assert req.selected_phases == [Phase.QA_TESTING, Phase.DEVELOPMENT]


def test_request_accepts_phase_string_values() -> None:
    # The wire format is JSON string values; Pydantic coerces them to the Phase enum. Built via
    # model_validate to mirror how an actual request body arrives (untyped JSON).
    req = CreateEstimateRequest.model_validate(
        {"raw_input": _RAW, "selected_phases": ["development", "qa_testing"]}
    )
    assert req.selected_phases == [Phase.DEVELOPMENT, Phase.QA_TESTING]


def test_request_rejects_empty_selection() -> None:
    # An explicit empty list is a client error (would otherwise skip every twin).
    with pytest.raises(ValidationError):
        CreateEstimateRequest(raw_input=_RAW, selected_phases=[])


def test_request_rejects_unknown_phase() -> None:
    with pytest.raises(ValidationError):
        CreateEstimateRequest.model_validate(
            {"raw_input": _RAW, "selected_phases": ["not_a_phase"]}
        )


# --- single-phase variance combine (the degenerate-subset MC path) -----------


def test_combine_range_single_phase_variance_path_is_finite() -> None:
    # The graph single-phase test exercises the stub path (std=None ⇒ comonotonic branch). This
    # drives the OTHER branch directly: one phase whose ranges carry Monte-Carlo std, so the
    # lognormal variance-combine runs for a single phase and must stay finite + ordered (the
    # degenerate case a one-phase subset would hit on the real MC path).
    phase = PhaseEstimate(
        phase=Phase.DEVELOPMENT,
        twin_name="dev",
        algorithm="test",
        ai_assisted_hours=HourRange(
            optimistic=80, most_likely=100, pessimistic=160, std=25.0, mean=104.0
        ),
        manual_only_hours=HourRange(
            optimistic=120, most_likely=150, pessimistic=230, std=35.0, mean=156.0
        ),
        ai_assisted_role_hours=[],
        manual_only_role_hours=[],
        gaps=[],
        confidence=0.7,
    )

    for ai in (True, False):
        combined = _combine_range([phase], ai=ai)
        # std present ⇒ we took the lognormal variance-combine branch, not the comonotonic fallback.
        assert combined.std is not None
        assert combined.percentiles is not None
        assert math.isfinite(combined.optimistic) and math.isfinite(combined.pessimistic)
        assert 0 < combined.optimistic <= combined.most_likely <= combined.pessimistic
        assert all(math.isfinite(v) for v in combined.percentiles.values())

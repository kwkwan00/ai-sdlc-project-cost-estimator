"""Smoke tests for the shared twin helpers. Real twin behavior is tested per-twin."""

from __future__ import annotations

from models.project_schema import RoleRoster
from models.twin_outputs import Phase
from orchestrator.nodes._twin_base import build_twin_user_prompt, stub_phase_estimate


def test_stub_phase_estimate_round_trips_through_pydantic() -> None:
    roster = RoleRoster.default()
    est = stub_phase_estimate(Phase.DISCOVERY, "discovery_analyst", "UCP", 200, 240, roster)
    assert est.phase is Phase.DISCOVERY
    assert est.ai_assisted_hours.most_likely == 200
    assert est.manual_only_hours.most_likely == 240
    # Role hours should sum to the input total (within float tolerance).
    assert abs(sum(rh.hours for rh in est.ai_assisted_role_hours) - 200) < 1e-6
    assert abs(sum(rh.hours for rh in est.manual_only_role_hours) - 240) < 1e-6
    # One row per roster role.
    assert len(est.ai_assisted_role_hours) == len(roster.roles)


def test_build_twin_user_prompt_includes_raw_and_pass_marker() -> None:
    state = {
        "raw_input": "Build a patient portal for a clinic.",
        "parsed_context": {"industry_hint": "healthcare"},
        "stage2": None,
        "stage3": None,
        "clarifying_questions": [],
    }
    prompt_p1 = build_twin_user_prompt(state, pass_num=1)
    assert "Pass 1" in prompt_p1
    assert "patient portal" in prompt_p1
    assert "healthcare" in prompt_p1


def test_build_twin_user_prompt_includes_calibration_for_matching_phase() -> None:
    state = {
        "raw_input": "Build a thing.",
        "parsed_context": {},
        "stage2": None,
        "stage3": None,
        "clarifying_questions": [],
        "calibration_examples": [
            {"phase": "discovery", "sample_count": 5, "avg_ai_assisted_mid": 120.0},
            {"phase": "development", "sample_count": 9, "avg_ai_assisted_mid": 1100.0},
        ],
    }
    prompt = build_twin_user_prompt(state, pass_num=1, phase_value="discovery")
    # Discovery row rendered; development row filtered out.
    assert '"avg_ai_assisted_mid": 120' in prompt
    assert "1100" not in prompt


def test_build_twin_user_prompt_omits_calibration_when_phase_value_unset() -> None:
    state = {
        "raw_input": "x",
        "parsed_context": {},
        "stage2": None,
        "stage3": None,
        "clarifying_questions": [],
        "calibration_examples": [
            {"phase": "discovery", "sample_count": 5, "avg_ai_assisted_mid": 120.0}
        ],
    }
    prompt = build_twin_user_prompt(state, pass_num=1)
    assert "calibration" not in prompt


def test_build_twin_user_prompt_pass2_includes_user_answers() -> None:
    from models.twin_outputs import ClarifyingQuestion

    state = {
        "raw_input": "Build something.",
        "parsed_context": {},
        "stage2": None,
        "stage3": None,
        "clarifying_questions": [
            ClarifyingQuestion(
                id="q1",
                text="How many integrations?",
                source_phases=[Phase.DEVELOPMENT],
                suggested_default="3",
                impact_hours=100,
                answered=True,
                answer="7",
            )
        ],
    }
    prompt_p2 = build_twin_user_prompt(state, pass_num=2)
    assert "Pass 2" in prompt_p2
    assert "7" in prompt_p2  # the user's answer

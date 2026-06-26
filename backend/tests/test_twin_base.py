"""Smoke tests for the shared twin helpers. Real twin behavior is tested per-twin."""

from __future__ import annotations

from models.project_schema import RoleRoster
from models.twin_outputs import Phase
from orchestrator.nodes._twin_base import (
    assemble_phase_estimate,
    build_twin_user_prompt,
    load_prompt,
    rate_by_role,
    risk_specs_from,
    risks_from_inputs,
    roster_for,
    stub_phase_estimate,
)


def test_load_prompt_is_cached_per_name() -> None:
    # Static prompts: a second call returns the SAME cached object (read once).
    first = load_prompt("development_architect")
    second = load_prompt("development_architect")
    assert first is second
    assert first.strip()  # non-empty prompt body


def test_roster_for_falls_back_to_default_when_stage2_absent() -> None:
    assert roster_for({"stage2": None}).roles == RoleRoster.default().roles
    # No stage2 key at all → still the default.
    assert roster_for({}).roles == RoleRoster.default().roles


def test_roster_for_returns_stage2_roster_when_populated() -> None:
    from models.project_schema import Stage2Context

    roster = RoleRoster.default()
    stage2 = Stage2Context(roster=roster)
    assert roster_for({"stage2": stage2}) is roster


def test_rate_by_role_maps_role_id_to_rate() -> None:
    roster = RoleRoster.default()
    rates = rate_by_role(roster)
    assert rates == {r.role_id: r.rate_per_hour for r in roster.roles}
    assert len(rates) == len(roster.roles)


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


def test_build_twin_user_prompt_includes_phase_scope_when_a_subset_is_selected() -> None:
    from models.twin_outputs import Phase

    state = {
        "raw_input": "Build a thing.",
        "parsed_context": {},
        "stage2": None,
        "stage3": None,
        "clarifying_questions": [],
        "selected_phases": [Phase.DEVELOPMENT, Phase.QA_TESTING],
    }
    prompt = build_twin_user_prompt(state, pass_num=1, phase_value="development")
    assert "phases_in_scope" in prompt
    assert "development" in prompt and "qa_testing" in prompt
    assert "out-of-scope phases" in prompt  # the scope instruction is present


def test_build_twin_user_prompt_omits_phase_scope_for_full_scope() -> None:
    state = {
        "raw_input": "Build a thing.",
        "parsed_context": {},
        "stage2": None,
        "stage3": None,
        "clarifying_questions": [],
        # No selected_phases ⇒ full scope ⇒ no scope note (request unchanged from pre-feature).
    }
    prompt = build_twin_user_prompt(state, pass_num=1, phase_value="development")
    assert "phases_in_scope" not in prompt


def test_build_twin_user_prompt_surfaces_technology_stack() -> None:
    from models.project_schema import Stage3Context

    state = {
        "raw_input": "Build a thing.",
        "parsed_context": {},
        "stage2": None,
        "stage3": Stage3Context(technology_stack="Legacy COBOL mainframe + DB2"),
        "clarifying_questions": [],
    }
    prompt = build_twin_user_prompt(state, pass_num=1)
    # The user-specified stack reaches the twin (it's an estimation signal it may use).
    assert "Legacy COBOL mainframe + DB2" in prompt
    assert "technology_stack" in prompt


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


def test_build_twin_user_prompt_injects_reduction_guardrail() -> None:
    from models.project_schema import AiToolingLevel, PhaseToolingLevels, Stage3Context

    state = {
        "raw_input": "Build a thing.",
        "parsed_context": {},
        "stage2": None,
        "stage3": Stage3Context(
            ai_tooling=PhaseToolingLevels(development=AiToolingLevel.AGENTIC)
        ),
        "clarifying_questions": [],
    }
    prompt = build_twin_user_prompt(state, pass_num=1, phase_value="development")
    # DEVELOPMENT/AGENTIC default band is (0.45, 0.72) → 45.0–72.0%.
    assert "ai_reduction_guardrail" in prompt
    assert '"min_pct": 45' in prompt
    assert '"max_pct": 72' in prompt
    assert '"tooling_level": "agentic"' in prompt


def test_build_twin_user_prompt_guardrail_respects_db_override() -> None:
    from models.project_schema import AiToolingLevel, PhaseToolingLevels, Stage3Context

    state = {
        "raw_input": "Build a thing.",
        "parsed_context": {},
        "stage2": None,
        "stage3": Stage3Context(
            ai_tooling=PhaseToolingLevels(development=AiToolingLevel.AGENTIC)
        ),
        "clarifying_questions": [],
        "reduction_bands": {"development": {"agentic": [0.30, 0.45]}},
    }
    prompt = build_twin_user_prompt(state, pass_num=1, phase_value="development")
    assert '"min_pct": 30' in prompt
    assert '"max_pct": 45' in prompt


def test_build_twin_user_prompt_omits_guardrail_when_no_tooling() -> None:
    # NONE tooling → band hi == 0 → nothing to propose, no guardrail block.
    state = {
        "raw_input": "Build a thing.",
        "parsed_context": {},
        "stage2": None,
        "stage3": None,  # defaults → all phases NONE
        "clarifying_questions": [],
    }
    prompt = build_twin_user_prompt(state, pass_num=1, phase_value="development")
    assert "ai_reduction_guardrail" not in prompt


class _FakeRisk:
    def __init__(self) -> None:
        self.description = "Schema churn"
        self.probability = 0.3
        self.impact_hours_low = 10.0
        self.impact_hours_high = 40.0


def test_risk_specs_from_maps_to_probability_low_high_tuples() -> None:
    specs = risk_specs_from([_FakeRisk()])
    assert specs == [(0.3, 10.0, 40.0)]


def test_risks_from_inputs_maps_probability_to_likelihood() -> None:
    risks = risks_from_inputs([_FakeRisk()])
    assert len(risks) == 1
    r = risks[0]
    assert r.description == "Schema churn"
    assert r.likelihood == 0.3
    assert r.impact_hours_low == 10.0
    assert r.impact_hours_high == 40.0


class _FakeInputs:
    def __init__(self) -> None:
        self.assumptions = ["Stable API"]
        self.risks = [_FakeRisk()]
        self.gaps = []
        self.confidence = 0.7
        self.notes = "ignored by assemble"


def _mc(point: float, p10: float, p90: float):
    from orchestrator.montecarlo import MCResult

    return MCResult(
        point=point,
        p10=p10,
        p50=point,
        p90=p90,
        mean=point,
        std=0.0,
        n=1,
        degenerate=True,
        percentiles={"p50": point},
    )


def test_assemble_phase_estimate_holds_invariants_and_assumption_factor() -> None:
    roster = RoleRoster.default()
    point_mid = 1000.0
    reduction = 0.25
    ai_mid = point_mid * (1 - reduction)
    manual_mc = _mc(point_mid, 800, 1400)
    ai_mc = _mc(ai_mid, 800 * (1 - reduction), 1400 * (1 - reduction))

    est = assemble_phase_estimate(
        phase=Phase.DEVELOPMENT,
        twin_name="development_architect",
        algorithm="COCOMO_II",
        point_mid=point_mid,
        ai_mid=ai_mid,
        manual_mc=manual_mc,
        ai_mc=ai_mc,
        roster=roster,
        inputs=_FakeInputs(),
        breakdown={"ksloc": 5.0},
        effective_reduction=reduction,
        assumption_impact_factor=0.05,
        notes="dev notes",
    )

    # AI mid is exactly manual mid × (1 − reduction).
    assert abs(est.ai_assisted_hours.most_likely - est.manual_only_hours.most_likely * (1 - reduction)) < 1e-6
    # Role hours sum to the deterministic mids.
    assert abs(sum(rh.hours for rh in est.manual_only_role_hours) - point_mid) < 1e-6
    assert abs(sum(rh.hours for rh in est.ai_assisted_role_hours) - ai_mid) < 1e-6
    # Per-twin assumption factor is honored (0.05 here).
    assert est.assumptions[0].impact_hours == point_mid * 0.05
    # Risk mapping + passthrough fields.
    assert est.risks[0].likelihood == 0.3
    assert est.effective_ai_reduction_pct == 25.0
    assert est.breakdown == {"ksloc": 5.0}
    assert est.notes == "dev notes"
    assert est.algorithm == "COCOMO_II"


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

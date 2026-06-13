"""Offline, deterministic tests for the evals harness.

These run with NO ANTHROPIC_API_KEY: every LLM (judge) call is monkeypatched. They
cover the applicability matrix, the deterministic correctness rubrics (one passing
and one failing construction each), the estimate_accuracy banding + skip path, the
judge-rubric score mapping, runner aggregation (including skipped-score exclusion),
and the reporter.
"""

from __future__ import annotations

import pytest

from evals import models, report, rubrics, runner
from evals.agents import _is_stub_estimate  # noqa: F401  (behavior under test)
from evals.models import (
    AGENT_RUBRICS,
    RUBRIC_THRESHOLDS,
    AgentSample,
    EvalCase,
)
from models.project_schema import AiToolingLevel, PhaseToolingLevels, RoleRoster
from models.twin_outputs import (
    Gap,
    HourRange,
    Phase,
    PhaseEstimate,
    RoleCategory,
    RoleHours,
    RoleSeniority,
)
from orchestrator.nodes._twin_base import stub_phase_estimate
from prefill import IndustryOption, NormalizedProjectContext, RegulatoryRequirement
from tooling_classifier import ToolingClassification

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _role(role_id: str, cat: RoleCategory, sen: RoleSeniority, hours: float) -> RoleHours:
    return RoleHours(
        role_id=role_id, role_description=role_id, category=cat, seniority=sen, hours=hours
    )


def _make_estimate(
    *,
    phase: Phase = Phase.DEVELOPMENT,
    manual_ml: float = 100.0,
    ai_ml: float = 70.0,
    reduction_pct: float = 30.0,
    ai_roles: list[RoleHours] | None = None,
    manual_roles: list[RoleHours] | None = None,
    breakdown: dict[str, float] | None = None,
) -> PhaseEstimate:
    """A PhaseEstimate whose hour points satisfy ai = manual×(1-r) by default."""
    return PhaseEstimate(
        phase=phase,
        twin_name=phase.value,
        algorithm="X",
        manual_only_hours=HourRange(
            optimistic=manual_ml * 0.8, most_likely=manual_ml, pessimistic=manual_ml * 1.3
        ),
        ai_assisted_hours=HourRange(
            optimistic=ai_ml * 0.8, most_likely=ai_ml, pessimistic=ai_ml * 1.3
        ),
        manual_only_role_hours=manual_roles
        or [_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, manual_ml)],
        ai_assisted_role_hours=ai_roles
        or [_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, ai_ml)],
        confidence=0.8,
        effective_ai_reduction_pct=reduction_pct,
        breakdown=breakdown or {},
    )


def _roster(*roles: tuple[str, RoleCategory, RoleSeniority, float]) -> RoleRoster:
    from models.project_schema import CustomRole

    return RoleRoster(
        roles=[
            CustomRole(role_id=rid, description=rid, category=c, seniority=s, percentage=p)
            for rid, c, s, p in roles
        ]
    )


def _twin_sample(
    est: PhaseEstimate,
    *,
    phase: Phase,
    tooling: str,
    roster: RoleRoster | None = None,
    gold: dict | None = None,
) -> AgentSample:
    r = roster or _roster(("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100.0))
    return AgentSample(
        case_id="c",
        agent=phase.value,
        output_obj=est,
        gold=gold or {},
        eval_context={
            "phase": phase.value,
            "tooling_level": tooling,
            "reduction_bands": {},
            "roster": r.model_dump(),
        },
    )


async def _fake_verdict(score_value: float):
    async def _call(*, system, user, response_model, tool_name, model, **kwargs):  # type: ignore[no-untyped-def]
        return response_model(reasoning="ok", score=score_value)

    return _call


# --------------------------------------------------------------------------- #
# Applicability matrix
# --------------------------------------------------------------------------- #


def test_agent_rubrics_matrix_matches_spec() -> None:
    twins = {"discovery", "ux_design", "development", "code_review", "deployment", "qa_testing"}
    assert set(AGENT_RUBRICS) == set(models.ALL_AGENTS)
    assert len(AGENT_RUBRICS) == 10

    # The deleted RAG rubrics appear nowhere.
    for rubric_list in AGENT_RUBRICS.values():
        assert "context_precision" not in rubric_list
        assert "contextual_recall" not in rubric_list

    twin_set = [
        "json_correctness",
        "faithfulness",
        "band_adherence",
        "algorithm_conformance",
        "role_attribution_validity",
        "estimate_accuracy",
    ]
    for agent in twins:
        assert AGENT_RUBRICS[agent] == twin_set

    assert AGENT_RUBRICS["prefill"] == ["summarization", "extraction_accuracy"]
    assert AGENT_RUBRICS["roster"] == ["plan_quality", "faithfulness", "staffing_adequacy"]
    assert AGENT_RUBRICS["tooling"] == ["classification_accuracy", "enum_constraint_adherence"]
    assert AGENT_RUBRICS["consolidator"] == ["plan_quality", "partition_correctness"]


# --------------------------------------------------------------------------- #
# json_correctness
# --------------------------------------------------------------------------- #


async def test_json_correctness_valid_estimate_scores_one() -> None:
    est = stub_phase_estimate(Phase.DISCOVERY, "discovery", "UCP", 200, 240, RoleRoster.default())
    sample = AgentSample(case_id="c1", agent="discovery", output_obj=est, is_stub=False)
    result = await rubrics.score("json_correctness", sample, judge_model="x")
    assert result.score == 1.0
    assert result.passed is True


async def test_json_correctness_stub_flagged_fails() -> None:
    est = stub_phase_estimate(Phase.DISCOVERY, "discovery", "UCP", 200, 240, RoleRoster.default())
    sample = AgentSample(case_id="c1", agent="discovery", output_obj=est, is_stub=True)
    result = await rubrics.score("json_correctness", sample, judge_model="x")
    assert result.passed is False
    assert "stub" in result.reasoning.lower()


async def test_json_correctness_missing_output_scores_zero() -> None:
    sample = AgentSample(case_id="c1", agent="qa_testing", output_obj=None, error="boom")
    result = await rubrics.score("json_correctness", sample, judge_model="x")
    assert result.score == 0.0
    assert result.passed is False


# --------------------------------------------------------------------------- #
# band_adherence
# --------------------------------------------------------------------------- #


async def test_band_adherence_passes_within_band() -> None:
    # development+agentic band is (0.36, 0.66); 30% is inside. ai<manual consistent.
    est = _make_estimate(phase=Phase.DEVELOPMENT, manual_ml=100, ai_ml=70, reduction_pct=30)
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is True


async def test_band_adherence_fails_reduction_above_band() -> None:
    # 80% reduction exceeds development+agentic hi (0.66).
    est = _make_estimate(phase=Phase.DEVELOPMENT, manual_ml=100, ai_ml=20, reduction_pct=80)
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is False
    assert "outside" in result.reasoning


async def test_band_adherence_fails_sign_inconsistency() -> None:
    # Positive reduction but AI scenario is HEAVIER than manual — contradiction.
    est = _make_estimate(phase=Phase.DEVELOPMENT, manual_ml=70, ai_ml=100, reduction_pct=30)
    # ai = manual×(1-r) won't hold, but band_adherence only checks sign here.
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is False


async def test_band_adherence_none_tooling_requires_zero_reduction() -> None:
    # NONE tooling → hi=0 → any nonzero reduction must fail.
    est = _make_estimate(phase=Phase.DISCOVERY, manual_ml=100, ai_ml=80, reduction_pct=20)
    sample = _twin_sample(est, phase=Phase.DISCOVERY, tooling="none")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is False

    est_zero = _make_estimate(phase=Phase.DISCOVERY, manual_ml=100, ai_ml=100, reduction_pct=0)
    sample_zero = _twin_sample(est_zero, phase=Phase.DISCOVERY, tooling="none")
    ok = await rubrics.score("band_adherence", sample_zero, judge_model="x")
    assert ok.passed is True


async def test_band_adherence_autocomplete_on_discovery_has_no_band() -> None:
    # discovery has no AUTOCOMPLETE band → reduction must be zero.
    est = _make_estimate(phase=Phase.DISCOVERY, manual_ml=100, ai_ml=90, reduction_pct=10)
    sample = _twin_sample(est, phase=Phase.DISCOVERY, tooling="autocomplete")
    result = await rubrics.score("band_adherence", sample, judge_model="x")
    assert result.passed is False


# --------------------------------------------------------------------------- #
# algorithm_conformance
# --------------------------------------------------------------------------- #


async def test_algorithm_conformance_passes_identity() -> None:
    est = _make_estimate(manual_ml=100, ai_ml=70, reduction_pct=30, breakdown={"sloc": 1000.0})
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("algorithm_conformance", sample, judge_model="x")
    assert result.passed is True


async def test_algorithm_conformance_fails_broken_identity() -> None:
    # ai_ml far from manual×(1-r): r=30% so expected 70, but ai_ml=40.
    est = _make_estimate(manual_ml=100, ai_ml=40, reduction_pct=30)
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("algorithm_conformance", sample, judge_model="x")
    assert result.passed is False
    assert result.score < 1.0


async def test_algorithm_conformance_fails_negative_breakdown() -> None:
    est = _make_estimate(manual_ml=100, ai_ml=70, reduction_pct=30, breakdown={"bad": -5.0})
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("algorithm_conformance", sample, judge_model="x")
    assert result.passed is False


# --------------------------------------------------------------------------- #
# role_attribution_validity
# --------------------------------------------------------------------------- #


async def test_role_attribution_passes() -> None:
    roster = _roster(
        ("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 60.0),
        ("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 40.0),
    )
    est = _make_estimate(
        phase=Phase.DEVELOPMENT,
        manual_ml=100,
        ai_ml=70,
        reduction_pct=30,
        manual_roles=[
            _role("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 60),
            _role("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 40),
        ],
        ai_roles=[
            _role("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 42),
            _role("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 28),
        ],
    )
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic", roster=roster)
    result = await rubrics.score("role_attribution_validity", sample, judge_model="x")
    assert result.passed is True


async def test_role_attribution_fails_sum_mismatch() -> None:
    est = _make_estimate(
        phase=Phase.DEVELOPMENT,
        manual_ml=100,
        ai_ml=70,
        reduction_pct=30,
        manual_roles=[_role("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 50)],  # != 100
    )
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic")
    result = await rubrics.score("role_attribution_validity", sample, judge_model="x")
    assert result.passed is False
    assert "sum" in result.reasoning


async def test_role_attribution_fails_unknown_role_id() -> None:
    roster = _roster(("known", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100.0))
    est = _make_estimate(
        phase=Phase.DEVELOPMENT,
        manual_ml=100,
        ai_ml=70,
        reduction_pct=30,
        manual_roles=[_role("ghost", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100)],
        ai_roles=[_role("ghost", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 70)],
    )
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic", roster=roster)
    result = await rubrics.score("role_attribution_validity", sample, judge_model="x")
    assert result.passed is False
    assert "not in roster" in result.reasoning


async def test_role_attribution_fails_code_review_junior_cap() -> None:
    roster = _roster(
        ("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 60.0),
        ("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 40.0),
    )
    # CODE_REVIEW caps juniors at 15%; here junior holds 40%.
    est = _make_estimate(
        phase=Phase.CODE_REVIEW,
        manual_ml=100,
        ai_ml=80,
        reduction_pct=20,
        manual_roles=[
            _role("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 60),
            _role("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 40),
        ],
        ai_roles=[
            _role("sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 48),
            _role("jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 32),
        ],
    )
    sample = _twin_sample(est, phase=Phase.CODE_REVIEW, tooling="agentic", roster=roster)
    result = await rubrics.score("role_attribution_validity", sample, judge_model="x")
    assert result.passed is False
    assert "junior" in result.reasoning


# --------------------------------------------------------------------------- #
# estimate_accuracy (banding + skip)
# --------------------------------------------------------------------------- #


async def test_estimate_accuracy_skips_without_targets() -> None:
    est = _make_estimate()
    sample = _twin_sample(est, phase=Phase.DEVELOPMENT, tooling="agentic", gold={})
    result = await rubrics.score("estimate_accuracy", sample, judge_model="x")
    assert result.skipped is True


async def test_estimate_accuracy_perfect_when_on_target() -> None:
    est = _make_estimate(manual_ml=203, ai_ml=164, reduction_pct=19)
    sample = _twin_sample(
        est,
        phase=Phase.DISCOVERY,
        tooling="chat",
        gold={"target_manual_ml": 203, "target_ai_ml": 164},
    )
    result = await rubrics.score("estimate_accuracy", sample, judge_model="x")
    assert result.score == pytest.approx(1.0)
    assert result.skipped is False


async def test_estimate_accuracy_zero_when_far_off() -> None:
    # rel_err >= 0.60 → 0.0 for both targets.
    est = _make_estimate(manual_ml=20, ai_ml=10, reduction_pct=50)
    sample = _twin_sample(
        est,
        phase=Phase.DISCOVERY,
        tooling="chat",
        gold={"target_manual_ml": 203, "target_ai_ml": 164},
    )
    result = await rubrics.score("estimate_accuracy", sample, judge_model="x")
    assert result.score == pytest.approx(0.0)


async def test_estimate_accuracy_banding_is_linear_midrange() -> None:
    # rel_err = 0.40 → (0.60-0.40)/(0.60-0.25) ≈ 0.571.
    est = _make_estimate(manual_ml=140, ai_ml=98, reduction_pct=30)
    sample = _twin_sample(
        est, phase=Phase.DEVELOPMENT, tooling="agentic", gold={"target_manual_ml": 100}
    )
    result = await rubrics.score("estimate_accuracy", sample, judge_model="x")
    assert result.score == pytest.approx((0.60 - 0.40) / (0.60 - 0.25), abs=1e-3)


# --------------------------------------------------------------------------- #
# extraction_accuracy (prefill)
# --------------------------------------------------------------------------- #


def _prefill_obj(
    *, industry: IndustryOption, screens: int, integrations: list[str], regs: list[RegulatoryRequirement]
) -> NormalizedProjectContext:
    return NormalizedProjectContext(
        industry=industry,
        screen_count_estimate=screens,
        integrations=integrations,
        regulatory_requirements=regs,
        summary="s",
    )


async def test_extraction_accuracy_all_match() -> None:
    obj = _prefill_obj(
        industry=IndustryOption.HEALTHCARE,
        screens=25,
        integrations=["a", "b", "c", "d"],
        regs=[RegulatoryRequirement.HIPAA],
    )
    sample = AgentSample(
        case_id="c",
        agent="prefill",
        output_obj=obj,
        gold={
            "industry": "healthcare",
            "project_type": "greenfield",
            "regulatory_requirements": ["HIPAA"],
            "screen_count": 25,
            "integration_count": 4,
        },
    )
    result = await rubrics.score("extraction_accuracy", sample, judge_model="x")
    assert result.passed is True
    assert result.score == pytest.approx(1.0)


async def test_extraction_accuracy_fails_on_industry_mismatch() -> None:
    obj = _prefill_obj(
        industry=IndustryOption.FINTECH,  # wrong
        screens=25,
        integrations=["a", "b", "c", "d"],
        regs=[RegulatoryRequirement.HIPAA],
    )
    sample = AgentSample(
        case_id="c",
        agent="prefill",
        output_obj=obj,
        gold={
            "industry": "healthcare",
            "project_type": "greenfield",
            "regulatory_requirements": ["HIPAA"],
            "screen_count": 25,
            "integration_count": 4,
        },
    )
    result = await rubrics.score("extraction_accuracy", sample, judge_model="x")
    assert result.passed is False
    assert result.score < 1.0


# --------------------------------------------------------------------------- #
# staffing_adequacy (roster)
# --------------------------------------------------------------------------- #


class _FakeProposedRole:
    def __init__(self, category: RoleCategory, percentage: float) -> None:
        self.category = category
        self.percentage = percentage


class _FakeProposal:
    def __init__(self, roles: list[_FakeProposedRole]) -> None:
        self.roles = roles


async def test_staffing_adequacy_passes_with_required_categories() -> None:
    proposal = _FakeProposal(
        [
            _FakeProposedRole(RoleCategory.ENGINEERING, 40),
            _FakeProposedRole(RoleCategory.PRODUCT, 20),
            _FakeProposedRole(RoleCategory.UI_UX, 20),
            _FakeProposedRole(RoleCategory.QA, 20),
        ]
    )
    sample = AgentSample(
        case_id="c",
        agent="roster",
        output_obj=proposal,
        eval_context={"stage2_signals": {"screen_count": 25, "regulatory": ["HIPAA"]}},
    )
    result = await rubrics.score("staffing_adequacy", sample, judge_model="x")
    assert result.passed is True


async def test_staffing_adequacy_fails_missing_qa_when_regulated() -> None:
    proposal = _FakeProposal(
        [
            _FakeProposedRole(RoleCategory.ENGINEERING, 50),
            _FakeProposedRole(RoleCategory.PRODUCT, 30),
            _FakeProposedRole(RoleCategory.UI_UX, 20),
        ]
    )
    sample = AgentSample(
        case_id="c",
        agent="roster",
        output_obj=proposal,
        eval_context={"stage2_signals": {"screen_count": 25, "regulatory": ["HIPAA"]}},
    )
    result = await rubrics.score("staffing_adequacy", sample, judge_model="x")
    assert result.passed is False
    assert "qa" in result.reasoning


async def test_staffing_adequacy_fails_role_over_60pct() -> None:
    proposal = _FakeProposal(
        [
            _FakeProposedRole(RoleCategory.ENGINEERING, 70),  # > 60%
            _FakeProposedRole(RoleCategory.PRODUCT, 30),
        ]
    )
    sample = AgentSample(
        case_id="c",
        agent="roster",
        output_obj=proposal,
        eval_context={"stage2_signals": {"screen_count": 0, "regulatory": []}},
    )
    result = await rubrics.score("staffing_adequacy", sample, judge_model="x")
    assert result.passed is False
    assert "60%" in result.reasoning


# --------------------------------------------------------------------------- #
# classification_accuracy + enum_constraint_adherence (tooling)
# --------------------------------------------------------------------------- #


def _tooling_obj(**levels: str) -> ToolingClassification:
    return ToolingClassification(
        ai_tooling=PhaseToolingLevels(**{k: AiToolingLevel(v) for k, v in levels.items()})
    )


async def test_classification_accuracy_exact_match() -> None:
    obj = _tooling_obj(development="agentic", code_review="agentic", ux_design="chat")
    sample = AgentSample(
        case_id="c",
        agent="tooling",
        output_obj=obj,
        gold={
            "ai_tooling": {
                "discovery": "none",
                "ux_design": "chat",
                "development": "agentic",
                "code_review": "agentic",
                "deployment": "none",
                "qa_testing": "none",
            }
        },
    )
    result = await rubrics.score("classification_accuracy", sample, judge_model="x")
    assert result.passed is True
    assert result.score == pytest.approx(1.0)


async def test_classification_accuracy_partial_mismatch() -> None:
    obj = _tooling_obj(development="chat")  # gold wants agentic
    sample = AgentSample(
        case_id="c",
        agent="tooling",
        output_obj=obj,
        gold={
            "ai_tooling": {
                "discovery": "none",
                "ux_design": "none",
                "development": "agentic",
                "code_review": "none",
                "deployment": "none",
                "qa_testing": "none",
            }
        },
    )
    result = await rubrics.score("classification_accuracy", sample, judge_model="x")
    assert result.passed is False
    assert result.score == pytest.approx(5 / 6)


async def test_enum_constraint_adherence_passes_clean() -> None:
    obj = _tooling_obj(development="autocomplete", deployment="autocomplete")
    sample = AgentSample(case_id="c", agent="tooling", output_obj=obj)
    result = await rubrics.score("enum_constraint_adherence", sample, judge_model="x")
    assert result.passed is True


async def test_enum_constraint_adherence_fails_autocomplete_on_discovery() -> None:
    obj = _tooling_obj(discovery="autocomplete")
    sample = AgentSample(case_id="c", agent="tooling", output_obj=obj)
    result = await rubrics.score("enum_constraint_adherence", sample, judge_model="x")
    assert result.passed is False
    assert "autocomplete" in result.reasoning


# --------------------------------------------------------------------------- #
# partition_correctness (consolidator)
# --------------------------------------------------------------------------- #


def _gap(topic: str) -> Gap:
    return Gap(topic=topic, question_text=topic, impact_hours=10, suggested_default="d")


async def test_partition_correctness_passes() -> None:
    # 3 merged outputs, gold expects 3 clusters; phase coverage preserved.
    merged = [
        (_gap("a"), [Phase.DEVELOPMENT]),
        (_gap("b"), [Phase.DEPLOYMENT]),
        (_gap("c"), [Phase.QA_TESTING]),
    ]
    sample = AgentSample(
        case_id="c",
        agent="consolidator",
        output_obj=merged,
        gold={"clusters": [[0], [1], [2]]},
        eval_context={
            "input_phases": [["development"], ["deployment"], ["qa_testing"]]
        },
    )
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.passed is True


async def test_partition_correctness_fails_wrong_cluster_count() -> None:
    merged = [(_gap("a"), [Phase.DEVELOPMENT]), (_gap("b"), [Phase.DEPLOYMENT])]
    sample = AgentSample(
        case_id="c",
        agent="consolidator",
        output_obj=merged,
        gold={"clusters": [[0, 1], [2]]},  # expects 2 clusters but from 3 inputs
        eval_context={"input_phases": [["development"], ["deployment"]]},
    )
    # output count 2 == gold count 2 here, so make a real mismatch:
    sample.gold = {"clusters": [[0], [1], [2]]}  # expects 3
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.passed is False
    assert "count" in result.reasoning


async def test_partition_correctness_fails_dropped_phase() -> None:
    merged = [(_gap("a"), [Phase.DEVELOPMENT]), (_gap("b"), [Phase.DEPLOYMENT])]
    sample = AgentSample(
        case_id="c",
        agent="consolidator",
        output_obj=merged,
        gold={"clusters": [[0], [1]]},
        eval_context={
            # qa_testing in inputs but never appears in merged output → dropped.
            "input_phases": [["development"], ["deployment", "qa_testing"]]
        },
    )
    result = await rubrics.score("partition_correctness", sample, judge_model="x")
    assert result.passed is False
    assert "dropped" in result.reasoning


# --------------------------------------------------------------------------- #
# Judge rubrics (monkeypatched call_structured)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rubric", ["faithfulness", "plan_quality", "summarization"])
async def test_judge_rubric_maps_verdict_to_score(monkeypatch, rubric) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "call_structured", await _fake_verdict(0.9))
    sample = AgentSample(
        case_id="c1",
        agent="prefill",
        task_input="t",
        output_text="o",
        retrieval_context=["item"],
        source_text="src",
        expected_output="ref",
    )
    result = await rubrics.score(rubric, sample, judge_model="judge")
    assert result.score == 0.9
    assert result.passed is (0.9 >= RUBRIC_THRESHOLDS[rubric])


async def test_judge_rubric_below_threshold_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "call_structured", await _fake_verdict(0.4))
    sample = AgentSample(case_id="c1", agent="roster", expected_output="ref")
    result = await rubrics.score("plan_quality", sample, judge_model="judge")
    assert result.score == 0.4
    assert result.passed is False


async def test_judge_rubric_error_is_captured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def _boom(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("no api key")

    monkeypatch.setattr(rubrics, "call_structured", _boom)
    sample = AgentSample(case_id="c1", agent="roster", expected_output="ref")
    result = await rubrics.score("faithfulness", sample, judge_model="judge")
    assert result.score == 0.0
    assert result.passed is False
    assert result.error is not None


# --------------------------------------------------------------------------- #
# Runner aggregation (stubbed adapters + judge) + skipped exclusion
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    def __init__(self, agent: str) -> None:
        self.agent = agent

    async def run(self, case: EvalCase) -> AgentSample:
        output_obj = None
        eval_context: dict = {}
        gold: dict = case.gold
        if self.agent in models.TWIN_AGENTS:
            # A valid estimate that satisfies all deterministic twin invariants for
            # development+agentic (band 0.36-0.66, here r=40%).
            output_obj = _make_estimate(
                phase=Phase.DEVELOPMENT, manual_ml=100, ai_ml=60, reduction_pct=40
            )
            eval_context = {
                "phase": "development",
                "tooling_level": "agentic",
                "reduction_bands": {},
                "roster": _roster(
                    ("r1", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100.0)
                ).model_dump(),
            }
        return AgentSample(
            case_id=case.id,
            agent=self.agent,
            task_input="task",
            output_text="output",
            output_obj=output_obj,
            retrieval_context=["ctx"],
            source_text="src" if self.agent == "prefill" else None,
            expected_output=case.expected_output,
            gold=gold,
            eval_context=eval_context,
        )


async def test_run_evals_aggregates_and_respects_matrix(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "call_structured", await _fake_verdict(0.85))
    monkeypatch.setattr(
        runner,
        "ADAPTERS",
        {"development": _FakeAdapter("development"), "prefill": _FakeAdapter("prefill")},
    )

    def _fake_load(agent: str | None = None):  # type: ignore[no-untyped-def]
        cases = {
            "development": [EvalCase(id="d1", agent="development", expected_output="ref")],
            "prefill": [
                EvalCase(
                    id="p1",
                    agent="prefill",
                    expected_output="ref",
                    gold={
                        "industry": "healthcare",
                        "project_type": "greenfield",
                        "regulatory_requirements": [],
                        "screen_count": 0,
                        "integration_count": 0,
                    },
                )
            ],
        }
        return cases.get(agent, []) if agent else []

    monkeypatch.setattr(runner, "load_cases", _fake_load)

    rep = await runner.run_evals(
        agents=["development", "prefill"], rubrics=None, judge_model="judge", concurrency=2
    )
    by_agent = {a.agent: a for a in rep.agents}

    # development twin: json_correctness + the deterministic correctness rubrics all
    # present EXCEPT estimate_accuracy, which SKIPS (no targets) → excluded from means.
    dev_means = by_agent["development"].rubric_means
    assert "json_correctness" in dev_means
    assert by_agent["development"].rubric_means["json_correctness"] == 1.0
    assert by_agent["development"].rubric_means["band_adherence"] == 1.0
    assert "estimate_accuracy" not in dev_means  # skipped → not aggregated
    assert by_agent["development"].rubric_means["faithfulness"] == pytest.approx(0.85)

    # prefill: summarization (judge) + extraction_accuracy (deterministic).
    pf_means = by_agent["prefill"].rubric_means
    assert set(pf_means) == {"summarization", "extraction_accuracy"}
    assert "json_correctness" not in pf_means

    assert 0.0 <= rep.overall_pass_rate <= 1.0


async def test_run_evals_respects_requested_rubric_filter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "call_structured", await _fake_verdict(0.9))
    monkeypatch.setattr(runner, "ADAPTERS", {"development": _FakeAdapter("development")})
    monkeypatch.setattr(
        runner,
        "load_cases",
        lambda agent=None: [EvalCase(id="d1", agent="development", expected_output="r")]
        if agent == "development"
        else [],
    )
    rep = await runner.run_evals(
        agents=["development"], rubrics=["faithfulness"], judge_model="judge", concurrency=1
    )
    assert set(rep.agents[0].rubric_means) == {"faithfulness"}


def test_skipped_scores_excluded_from_report_means() -> None:
    from evals.models import AgentReport, CaseResult, EvalReport, RubricScore

    scores = [
        RubricScore(rubric="estimate_accuracy", score=0.0, passed=True, skipped=True),
        RubricScore(rubric="band_adherence", score=1.0, passed=True),
    ]
    rep = EvalReport(
        judge_model="j",
        agents=[
            AgentReport(
                agent="development",
                case_count=1,
                results=[CaseResult(case_id="c", agent="development", scores=scores)],
            )
        ],
    )
    means = rep.rubric_means()
    assert "estimate_accuracy" not in means  # skipped excluded
    assert means["band_adherence"] == 1.0
    assert rep.overall_pass_rate == 1.0  # only the non-skipped score counts


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #


async def test_report_renders_and_serializes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rubrics, "call_structured", await _fake_verdict(0.9))
    monkeypatch.setattr(runner, "ADAPTERS", {"development": _FakeAdapter("development")})
    monkeypatch.setattr(
        runner,
        "load_cases",
        lambda agent=None: [EvalCase(id="d1", agent="development", expected_output="r")]
        if agent == "development"
        else [],
    )
    rep = await runner.run_evals(
        agents=["development"], rubrics=None, judge_model="judge", concurrency=1
    )
    text = report.render_text(rep)
    assert "Eval report" in text
    assert "development" in text
    assert "Overall pass-rate" in text

    payload = report.to_dict(rep)
    assert payload["judge_model"] == "judge"
    assert "overall_pass_rate" in payload
    assert "rubric_means" in payload
    assert isinstance(payload["agents"], list)

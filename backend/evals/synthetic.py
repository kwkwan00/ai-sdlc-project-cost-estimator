"""Deterministic synthetic-project simulator for the eval harness.

``generate_cases(n, seed)`` fabricates ``n`` synthetic projects and emits one twin
``EvalCase`` per phase per project (``6 * n`` cases total), with NO LLM call so the
whole thing is reproducible and offline-testable.

For each project we:

1. **Sample ground-truth project parameters** from a seeded ``random.Random``: a true
   size (use cases, screens, SLOC/FP, reviewed KSLOC, CMP score, test-point function
   points), an industry + (maybe) a regulatory regime, a project type, a codebase
   context, and a per-phase AI tooling level.
2. **Render** those parameters into a plausible natural-language ``raw_input`` plus a
   structured ``parsed_context`` / ``stage2`` / ``stage3`` via plain string templates
   (no model, so the rendering is deterministic).
3. **Compute the GOLD actual hours** by feeding the TRUE inputs into each twin's OWN
   ``compute_*`` (the same deterministic algorithm the twin applies after extraction)
   and the TRUE AI reduction into ``effective_ai_reduction``. The gold is therefore
   "what the algorithm should produce given the true sizing":

       actual_manual_ml = compute_*(true_inputs)[0]
       eff              = effective_ai_reduction(true tooling/codebase/roster/...)
       actual_ai_ml     = actual_manual_ml * (1 - eff)

   stored as ``gold["actual_manual_ml"]`` / ``gold["actual_ai_ml"]`` on each case.

The point: a twin that correctly extracts the sizing inputs from the description should
produce a Monte-Carlo band that CONTAINS these true hours — exactly what the
``interval_calibration`` rubric checks. ``estimate_accuracy`` targets are set to the
same actuals so it scores too.

The roster used for the gold's AI-reduction is ``RoleRoster.default()``, and the same
roster is attached to each case's Stage 2, so the twin's ``effective_ai_reduction``
sees the identical seniority mix the gold was computed against.
"""

from __future__ import annotations

import random

from models.project_schema import (
    AiToolingLevel,
    CodebaseContext,
    PhaseToolingLevels,
    ProjectType,
    RoleRoster,
    Stage2Context,
    Stage3Context,
)
from models.twin_outputs import Phase
from orchestrator.ai_acceleration import effective_ai_reduction
from orchestrator.nodes.code_review_sentinel import CodeReviewInputs, compute_review_hours
from orchestrator.nodes.deployment_devops import CMPInputs, compute_cmp_hours
from orchestrator.nodes.development_architect import (
    DevCOCOMOInputs,
    StackCategory,
    compute_cocomo_hours,
)
from orchestrator.nodes.discovery_analyst import (
    AlignmentDifficulty,
    DecisionMakerAccessibility,
    DiscoveryUCPInputs,
    compute_ucp_hours,
)
from orchestrator.nodes.qa_testing_strategist import (
    QATPAInputs,
    auto_select_plan,
    compute_qa_hours,
)
from orchestrator.nodes.ux_design_strategist import UXSCPInputs, compute_scp_hours

from .models import EvalCase

# --------------------------------------------------------------------------- #
# Sampling tables (all deterministic given the seeded RNG).
# --------------------------------------------------------------------------- #

# (industry, project_type, regulatory) presets the NL templates read from.
_INDUSTRIES: list[tuple[str, list[str]]] = [
    ("healthcare", ["HIPAA"]),
    ("fintech", ["PCI-DSS", "SOC 2"]),
    ("insurance", ["SOC 2"]),
    ("retail", []),
    ("government", ["FedRAMP"]),
    ("education", ["FERPA"]),
]

_PROJECT_TYPES: list[ProjectType] = [
    ProjectType.GREENFIELD,
    ProjectType.ENHANCEMENT,
    ProjectType.INTEGRATION,
    ProjectType.LEGACY_REPLACEMENT,
]

# Codebase context, biased so most synthetic projects land somewhere a reduction
# actually applies (greenfield / small brownfield) rather than the net-negative tail.
_CODEBASES: list[CodebaseContext] = [
    CodebaseContext.GREENFIELD,
    CodebaseContext.GREENFIELD,
    CodebaseContext.BROWNFIELD_SMALL,
    CodebaseContext.BROWNFIELD_LARGE_UNFAMILIAR,
    CodebaseContext.BROWNFIELD_LARGE_FAMILIAR,
]

# Per-phase tooling pools. Discovery/UX/code_review have NO autocomplete band, so
# their pools exclude AUTOCOMPLETE (matching the enum_constraint_adherence rule).
_TOOLING_WITH_AUTOCOMPLETE: list[AiToolingLevel] = [
    AiToolingLevel.NONE,
    AiToolingLevel.AUTOCOMPLETE,
    AiToolingLevel.CHAT,
    AiToolingLevel.AGENTIC,
]
_TOOLING_NO_AUTOCOMPLETE: list[AiToolingLevel] = [
    AiToolingLevel.NONE,
    AiToolingLevel.CHAT,
    AiToolingLevel.AGENTIC,
]

_LANGUAGES: list[str] = ["typescript", "python", "java", "go", "csharp"]

_STACKS: list[StackCategory] = [
    StackCategory.MODERN_WEB,
    StackCategory.JVM_ENTERPRISE,
    StackCategory.DOTNET,
    StackCategory.DATA_ML,
    StackCategory.MOBILE_CROSS_PLATFORM,
]


# --------------------------------------------------------------------------- #
# Ground-truth project parameters.
# --------------------------------------------------------------------------- #


class _TrueProject:
    """The sampled ground-truth parameters for one synthetic project.

    Holds the (deterministically derived) per-twin ``*Inputs`` models so the NL
    template, the structured Stage 2/3 context, and the gold ``compute_*`` all read
    from a single coherent source of truth.
    """

    def __init__(self, idx: int, rng: random.Random) -> None:
        self.idx = idx
        self.industry, self.regulatory = rng.choice(_INDUSTRIES)
        self.project_type = rng.choice(_PROJECT_TYPES)
        self.codebase = rng.choice(_CODEBASES)
        self.language = rng.choice(_LANGUAGES)
        self.stack = rng.choice(_STACKS)
        self.regulated = bool(self.regulatory)

        # Per-phase tooling.
        self.tooling: dict[Phase, AiToolingLevel] = {
            Phase.DISCOVERY: rng.choice(_TOOLING_NO_AUTOCOMPLETE),
            Phase.UX_DESIGN: rng.choice(_TOOLING_NO_AUTOCOMPLETE),
            Phase.DEVELOPMENT: rng.choice(_TOOLING_WITH_AUTOCOMPLETE),
            Phase.CODE_REVIEW: rng.choice(_TOOLING_NO_AUTOCOMPLETE),
            Phase.DEPLOYMENT: rng.choice(_TOOLING_WITH_AUTOCOMPLETE),
            Phase.QA_TESTING: rng.choice(_TOOLING_WITH_AUTOCOMPLETE),
        }

        # ---- True size drivers (coherent across phases) ---- #
        self.screen_count = rng.randint(8, 48)
        self.integration_count = rng.randint(0, 8)
        self.user_role_count = rng.randint(2, 6)
        # Function points + the SLOC they imply for the chosen language.
        self.function_points = float(rng.randint(150, 900))
        self.sloc = float(round(self.function_points * _SLOC_PER_FP.get(self.language, 47)))
        self.reviewed_ksloc = round(self.sloc / 1000.0, 2)

        # Build each twin's TRUE inputs from the shared drivers.
        self.discovery_inputs = self._build_discovery(rng)
        self.ux_inputs = self._build_ux(rng)
        self.dev_inputs = self._build_dev(rng)
        self.review_inputs = self._build_review(rng)
        self.deployment_inputs = self._build_deployment(rng)
        self.qa_inputs = self._build_qa(rng)

        self.roster = RoleRoster.default()

    # -- per-twin TRUE input builders -- #

    def _split3(self, total: int, rng: random.Random) -> tuple[int, int, int]:
        """Partition ``total`` items into (simple, average, complex) buckets."""
        a = rng.randint(0, total)
        b = rng.randint(0, total - a)
        c = total - a - b
        return a, b, c

    def _build_discovery(self, rng: random.Random) -> DiscoveryUCPInputs:
        use_cases = self.screen_count + rng.randint(0, 8)
        s, av, cx = self._split3(use_cases, rng)
        sa, aa, ca = self._split3(self.user_role_count, rng)
        return DiscoveryUCPInputs(
            simple_use_cases=s,
            average_use_cases=av,
            complex_use_cases=cx,
            simple_actors=sa,
            average_actors=aa,
            complex_actors=ca,
            tfactor=rng.randint(15, 45),
            efactor=rng.randint(8, 30),
            stakeholder_group_count=rng.randint(1, 7),
            decision_maker_accessibility=rng.choice(list(DecisionMakerAccessibility)),
            alignment_difficulty=rng.choice(list(AlignmentDifficulty)),
            phase_ratio_hint=round(rng.uniform(0.06, 0.12), 3),
            productivity_factor=round(rng.uniform(20.0, 30.0), 1),
            confidence=round(rng.uniform(0.6, 0.9), 2),
        )

    def _build_ux(self, rng: random.Random) -> UXSCPInputs:
        s, av, cx = self._split3(self.screen_count, rng)
        novel = rng.randint(0, max(1, self.screen_count // 8))
        return UXSCPInputs(
            simple_screens=s,
            average_screens=av,
            complex_screens=cx,
            novel_screens=novel,
            design_system_factor=round(rng.uniform(0.7, 1.2), 2),
            interaction_complexity_multiplier=round(rng.uniform(1.0, 1.4), 2),
            iteration_factor=round(rng.uniform(1.1, 2.0), 2),
            is_responsive=rng.random() < 0.7,
            confidence=round(rng.uniform(0.6, 0.9), 2),
        )

    def _build_dev(self, rng: random.Random) -> DevCOCOMOInputs:
        return DevCOCOMOInputs(
            sloc_estimate=self.sloc,
            primary_language=self.language,
            scale_factor_sum=rng.randint(6, 20),
            eaf_composite=round(rng.uniform(0.8, 1.5), 2),
            stack_category=self.stack,
            infrastructure_leverage_pct=round(rng.uniform(0.0, 30.0), 1),
            confidence=round(rng.uniform(0.6, 0.9), 2),
        )

    def _build_review(self, rng: random.Random) -> CodeReviewInputs:
        return CodeReviewInputs(
            total_ksloc=self.reviewed_ksloc,
            primary_language=self.language,
            kickback_rate_pct=round(rng.uniform(10.0, 40.0), 1),
            pr_complexity_factor=round(rng.uniform(0.8, 1.5), 2),
            tooling_setup_hours=round(rng.uniform(0.0, 80.0), 1),
            confidence=round(rng.uniform(0.6, 0.9), 2),
        )

    def _build_deployment(self, rng: random.Random) -> CMPInputs:
        return CMPInputs(
            cmp_score=round(rng.uniform(1.2, 2.8), 2),
            cicd_components=rng.randint(0, 10),
            monitoring_components=rng.randint(0, 8),
            handoff_hours=round(rng.uniform(20.0, 120.0), 1),
            regulatory_multiplier=1.25 if self.regulated else 1.0,
            conservative_bias_pct=round(rng.uniform(8.0, 20.0), 1),
            confidence=round(rng.uniform(0.6, 0.9), 2),
        )

    def _build_qa(self, rng: random.Random) -> QATPAInputs:
        has_ai = self.tooling[Phase.QA_TESTING] is not AiToolingLevel.NONE
        plan = auto_select_plan(has_ai, self.regulated)
        return QATPAInputs(
            total_function_points=self.function_points,
            df_weighted=round(rng.uniform(0.8, 1.3), 2),
            qd_score=round(rng.uniform(8.0, 20.0), 1),
            qi_score=round(rng.uniform(16.0, 80.0), 1),
            supplementary_hours=round(rng.uniform(80.0, 300.0), 1),
            has_ai_features=has_ai,
            has_regulatory_requirements=self.regulated,
            recommended_plan=plan,
            confidence=round(rng.uniform(0.6, 0.9), 2),
        )


# SLOC per Function Point, mirrored from the development twin (kept local so the
# synthetic module does not depend on the twin's private dict layout).
_SLOC_PER_FP: dict[str, int] = {
    "javascript": 47,
    "typescript": 47,
    "python": 32,
    "java": 53,
    "csharp": 53,
    "go": 40,
}


# --------------------------------------------------------------------------- #
# Gold computation: run each twin's own compute_* on the TRUE inputs.
# --------------------------------------------------------------------------- #


def _phase_gold(proj: _TrueProject, phase: Phase) -> tuple[float, float]:
    """``(actual_manual_ml, actual_ai_ml)`` for one phase, from the twin's own
    deterministic algorithm on the TRUE inputs and the TRUE effective AI reduction."""
    compute = {
        Phase.DISCOVERY: lambda: compute_ucp_hours(proj.discovery_inputs),
        Phase.UX_DESIGN: lambda: compute_scp_hours(proj.ux_inputs),
        Phase.DEVELOPMENT: lambda: compute_cocomo_hours(proj.dev_inputs),
        Phase.CODE_REVIEW: lambda: compute_review_hours(proj.review_inputs),
        Phase.DEPLOYMENT: lambda: compute_cmp_hours(proj.deployment_inputs),
        Phase.QA_TESTING: lambda: compute_qa_hours(proj.qa_inputs),
    }[phase]
    manual_ml = compute()[0]

    proposed = _proposed_reduction_for(proj, phase)
    eff = effective_ai_reduction(
        phase=phase,
        tooling=proj.tooling[phase],
        codebase=proj.codebase,
        roster=proj.roster,
        proposed_reduction=proposed,
        regulated=proj.regulated,
    )
    ai_ml = manual_ml * (1.0 - eff)
    return round(manual_ml, 2), round(ai_ml, 2)


def _proposed_reduction_for(proj: _TrueProject, phase: Phase) -> float | None:
    """The twin-proposed reduction (0..1) the gold uses. Discovery/UX propose none
    (the band midpoint is used); the four code-touching phases here propose None too,
    so the gold uses the guardrail midpoint — matching the deterministic mid the twin
    anchors on. Returning None keeps the gold tied to the band, not to an arbitrary
    sampled proposal the NL text cannot convey."""
    return None


# --------------------------------------------------------------------------- #
# NL + structured-context rendering (templated, deterministic).
# --------------------------------------------------------------------------- #

_TOOLING_PHRASE: dict[AiToolingLevel, str] = {
    AiToolingLevel.NONE: "no AI tooling",
    AiToolingLevel.AUTOCOMPLETE: "inline AI autocomplete",
    AiToolingLevel.CHAT: "an AI chat assistant",
    AiToolingLevel.AGENTIC: "an agentic AI coding workflow",
}

_CODEBASE_PHRASE: dict[CodebaseContext, str] = {
    CodebaseContext.GREENFIELD: "a brand-new greenfield codebase",
    CodebaseContext.BROWNFIELD_SMALL: "a small existing codebase",
    CodebaseContext.BROWNFIELD_LARGE_UNFAMILIAR: "a large unfamiliar legacy codebase",
    CodebaseContext.BROWNFIELD_LARGE_FAMILIAR: "a large codebase the team knows well",
}


def _render_raw_input(proj: _TrueProject) -> str:
    reg = (
        f" It must meet {', '.join(proj.regulatory)} compliance."
        if proj.regulatory
        else ""
    )
    integ = (
        f" It integrates with {proj.integration_count} external systems."
        if proj.integration_count
        else " It has no external integrations."
    )
    return (
        f"Project #{proj.idx}: a {proj.industry} {proj.project_type.value} application built on "
        f"{_CODEBASE_PHRASE[proj.codebase]} in {proj.language}.{reg} "
        f"It has roughly {proj.screen_count} screens across {proj.user_role_count} user roles.{integ} "
        f"We estimate about {int(proj.function_points)} function points "
        f"(~{int(proj.sloc):,} lines of {proj.language}). "
        f"Development uses {_TOOLING_PHRASE[proj.tooling[Phase.DEVELOPMENT]]}; "
        f"code review uses {_TOOLING_PHRASE[proj.tooling[Phase.CODE_REVIEW]]}; "
        f"QA uses {_TOOLING_PHRASE[proj.tooling[Phase.QA_TESTING]]}."
    )


def _parsed_context(proj: _TrueProject) -> dict[str, object]:
    return {
        "industry_hint": proj.industry,
        "summary": (
            f"{proj.industry} {proj.project_type.value} app, {proj.screen_count} screens, "
            f"{proj.integration_count} integrations, ~{int(proj.function_points)} FP."
        ),
        "screen_count": proj.screen_count,
        "integration_count": proj.integration_count,
        "user_role_count": proj.user_role_count,
        "function_points": proj.function_points,
        "sloc_estimate": proj.sloc,
    }


def _stage2(proj: _TrueProject) -> Stage2Context:
    return Stage2Context(
        industry=proj.industry,
        project_type=proj.project_type,
        screen_count_estimate=proj.screen_count,
        integration_count=proj.integration_count,
        regulatory_requirements=list(proj.regulatory),
        roster=proj.roster,
    )


def _stage3(proj: _TrueProject) -> Stage3Context:
    return Stage3Context(
        codebase_context=proj.codebase,
        ai_tooling=PhaseToolingLevels(
            discovery=proj.tooling[Phase.DISCOVERY],
            ux_design=proj.tooling[Phase.UX_DESIGN],
            development=proj.tooling[Phase.DEVELOPMENT],
            code_review=proj.tooling[Phase.CODE_REVIEW],
            deployment=proj.tooling[Phase.DEPLOYMENT],
            qa_testing=proj.tooling[Phase.QA_TESTING],
        ),
    )


_PHASE_TO_AGENT: dict[Phase, str] = {
    Phase.DISCOVERY: "discovery",
    Phase.UX_DESIGN: "ux_design",
    Phase.DEVELOPMENT: "development",
    Phase.CODE_REVIEW: "code_review",
    Phase.DEPLOYMENT: "deployment",
    Phase.QA_TESTING: "qa_testing",
}


def _case_for_phase(proj: _TrueProject, phase: Phase) -> EvalCase:
    actual_manual, actual_ai = _phase_gold(proj, phase)
    stage2 = _stage2(proj)
    stage3 = _stage3(proj)
    agent = _PHASE_TO_AGENT[phase]
    return EvalCase(
        id=f"synthetic-{proj.idx:03d}-{agent}",
        agent=agent,
        input={
            "raw_input": _render_raw_input(proj),
            "parsed_context": _parsed_context(proj),
            "stage2": stage2.model_dump(mode="json"),
            "stage3": stage3.model_dump(mode="json"),
        },
        expected_output=(
            f"A {phase.value} estimate for synthetic project #{proj.idx} whose Monte-Carlo "
            f"band brackets the true algorithm hours (manual ~{actual_manual}h, "
            f"ai ~{actual_ai}h) given the stated sizing and AI tooling."
        ),
        gold={
            # interval_calibration reads these; estimate_accuracy reads the targets.
            "actual_manual_ml": actual_manual,
            "actual_ai_ml": actual_ai,
            "target_manual_ml": actual_manual,
            "target_ai_ml": actual_ai,
        },
        notes=(
            "Synthetic case: gold actuals = the twin's own compute_* run on the TRUE "
            "sampled inputs, with the TRUE effective AI reduction. Generated by "
            "evals.synthetic.generate_cases (deterministic, seeded; no LLM)."
        ),
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def generate_cases(n: int, seed: int = 0) -> list[EvalCase]:
    """Generate ``6 * n`` synthetic twin ``EvalCase``s (one per phase per project).

    Fully deterministic in ``(n, seed)``: the same arguments always yield byte-identical
    cases, so this is safe to fold into offline tests. ``n <= 0`` yields an empty list.
    """
    if n <= 0:
        return []
    rng = random.Random(seed)
    cases: list[EvalCase] = []
    for idx in range(n):
        proj = _TrueProject(idx, rng)
        for phase in Phase:
            cases.append(_case_for_phase(proj, phase))
    return cases


def generate_cases_by_agent(n: int, seed: int = 0) -> dict[str, list[EvalCase]]:
    """``generate_cases`` grouped into the ``{agent: [EvalCase, ...]}`` shape the runner
    folds in via its ``synthetic_cases`` argument."""
    grouped: dict[str, list[EvalCase]] = {}
    for case in generate_cases(n, seed):
        grouped.setdefault(case.agent, []).append(case)
    return grouped

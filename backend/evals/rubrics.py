"""Rubric scoring — deterministic correctness checks + a few Claude-as-judge rubrics.

The high-value checks here are DETERMINISTIC (no LLM): they recompute the project's
own oracles (``orchestrator.ai_acceleration.band_for`` for the AI-reduction bands,
``orchestrator.role_attribution`` for the phase caps/floors, the PERT identity for
the dual-scenario hours) and assert the agent's output conforms. Each returns a
``RubricScore`` naming the offending value in ``reasoning``.

Three rubrics remain LLM-judged (``faithfulness``, ``plan_quality``,
``summarization``); they reuse ``orchestrator.llm.call_structured`` — the same
forced tool-use plumbing the twins use — with a ``_Verdict`` response model. No new
dependency is introduced.

``score(...)`` dispatches by rubric name and never raises: a judge failure is
captured as a ``RubricScore`` with ``error`` set and ``score=0.0`` so one bad
sample can't abort the batch.
"""

from __future__ import annotations

import logging
import math

from pydantic import BaseModel, ConfigDict, Field

from models.project_schema import AiToolingLevel, RoleRoster
from models.twin_outputs import (
    Phase,
    PhaseEstimate,
    RoleCategory,
    RoleHours,
    RoleSeniority,
)
from orchestrator.ai_acceleration import NEGATIVE_FLOOR, band_for
from orchestrator.llm import call_structured

from .models import RUBRIC_THRESHOLDS, AgentSample, RubricName, RubricScore

logger = logging.getLogger(__name__)

_EPS = 1e-6


class _Verdict(BaseModel):
    """A judge's structured verdict. ``reasoning`` is filled BEFORE ``score`` so
    the model reasons step-by-step."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        description="Step-by-step justification for the score. Reason BEFORE scoring."
    )
    supported_claims: list[str] = Field(
        default_factory=list,
        description="Output claims judged supported by the context (optional).",
    )
    unsupported_claims: list[str] = Field(
        default_factory=list,
        description="Output claims NOT supported by the context — fabrications (optional).",
    )
    score: float = Field(ge=0, le=1, description="Final score in [0, 1].")


# --------------------------------------------------------------------------- #
# Judge prompts (inline constants).
# --------------------------------------------------------------------------- #

_FAITHFULNESS_PROMPT = """\
You are a strict evaluator of an AI agent's FAITHFULNESS to its grounding.

You are given:
- TASK INPUT: the task the agent was asked to perform.
- GROUNDING CONTEXT: the project description + structured context the agent was given.
- ACTUAL OUTPUT: what the agent produced.

Score, in [0, 1], how well the output's claims, assumptions, and proposed inputs are
SUPPORTED by the grounding context. Penalize HALLUCINATION: fabricated scope, invented
integrations/features, regulatory regimes not mentioned, or quantities (screen counts,
integration counts) not implied by the description. A high score means every material
claim is grounded; a low score means the output invents facts the context does not support.

NOTE on the AI reduction guardrail: a twin's `effective_ai_reduction_pct` is a
POST-SCALING system value. The proposed reduction is clamped into the guardrail band,
then the system scales it down by codebase context and team seniority, so the effective
value may legitimately fall BELOW the band's `min_pct` (even go negative). Do NOT treat
`effective_ai_reduction_pct` being below the guardrail minimum as a violation,
contradiction, or unfaithful claim — that is expected, correct behavior.

Steps:
1. Enumerate the output's material claims/assumptions/numbers.
2. For each, mark whether the grounding context supports it (supported_claims) or it is
   fabricated/contradicted (unsupported_claims).
3. Reason about the proportion grounded, then assign the score.

Reason step-by-step in `reasoning` BEFORE assigning `score`. Submit via the tool.
"""

_PLAN_QUALITY_PROMPT = """\
You are a GEval-style evaluator of an AI agent's OUTPUT quality.

You are given:
- TASK INPUT: the task + the context the agent had.
- GROUNDING CONTEXT: the discrete inputs the agent was given.
- ACTUAL OUTPUT: what the agent produced.
- EXPECTED OUTPUT: a reference describing what a good output should contain.

Score, in [0, 1], whether the actual output is a sound, internally consistent,
well-justified response to the task. For a ROSTER agent this is staffing soundness and
feasibility (sensible role mix, seniority balance, percentages, rationale tied to the
project's complexity). For the question CONSOLIDATOR this is clustering soundness +
merged-question quality (genuinely-overlapping questions merged, distinct ones kept
separate, merged wording covering every sub-ask).

Evaluation steps:
1. Consistency: are the figures, categories, and claims internally consistent?
2. Justification: is each major decision supported by the input/context?
3. Coverage: does the output address what the task and reference call for?
4. Soundness: would a domain expert find it defensible?

Weigh these, reason step-by-step in `reasoning`, then assign `score`. A perfect output
scores near 1.0; a plausible-but-flawed one mid-range; an unsound one low. Submit via the tool.
"""

_SUMMARIZATION_PROMPT = """\
You are a strict evaluator of a SUMMARY against its source text.

You are given:
- SOURCE TEXT: the original project description.
- SUMMARY: the produced summary.

Compute two sub-scores, then return their MINIMUM as `score`:
- ALIGNMENT: 1.0 if the summary contains NO hallucinated or contradicted facts
  (every claim is supported by the source); lower as fabrications/contradictions appear.
- COVERAGE: 1.0 if the summary retains the KEY facts of the source; lower as
  important facts are dropped. Weight COVERAGE on the project's PURPOSE and MAJOR
  CAPABILITIES (the industry, project type, core user-facing features, key
  integrations/external systems, distinct user roles, and regulatory requirements)
  — NOT on exhaustively enumerating every quantitative detail (exact screen counts)
  or naming every single integration; those granular facts are graded separately.

Steps:
1. Check alignment: list any unsupported/contradicted claims (unsupported_claims).
2. Check coverage: list key facts retained (supported_claims) and any dropped.
3. Reason about both sub-scores, then set `score = min(alignment, coverage)`.

Reason step-by-step in `reasoning` BEFORE assigning `score`. Submit via the tool.
"""

_PROMPTS: dict[RubricName, str] = {
    "faithfulness": _FAITHFULNESS_PROMPT,
    "plan_quality": _PLAN_QUALITY_PROMPT,
    "summarization": _SUMMARIZATION_PROMPT,
}

_JUDGE_RUBRICS: frozenset[str] = frozenset(_PROMPTS)


def _render_context(items: list[str]) -> str:
    if not items:
        return "(no context items provided)"
    return "\n".join(f"- {item}" for item in items)


def _build_user_message(rubric: RubricName, sample: AgentSample) -> str:
    """Render the sample into the user message for a given LLM rubric."""
    if rubric == "summarization":
        return (
            f"SOURCE TEXT:\n{sample.source_text or '(none)'}\n\n"
            f"SUMMARY:\n{sample.output_text or '(none)'}"
        )
    if rubric == "faithfulness":
        return (
            f"TASK INPUT:\n{sample.task_input}\n\n"
            f"GROUNDING CONTEXT:\n{_render_context(sample.retrieval_context)}\n\n"
            f"ACTUAL OUTPUT:\n{sample.output_text or '(none)'}"
        )
    # plan_quality
    return (
        f"TASK INPUT:\n{sample.task_input}\n\n"
        f"GROUNDING CONTEXT:\n{_render_context(sample.retrieval_context)}\n\n"
        f"ACTUAL OUTPUT:\n{sample.output_text or '(none)'}\n\n"
        f"EXPECTED OUTPUT:\n{sample.expected_output or '(none)'}"
    )


# --------------------------------------------------------------------------- #
# Shared deterministic helpers
# --------------------------------------------------------------------------- #


def _fail(rubric: RubricName, reasoning: str, score: float = 0.0) -> RubricScore:
    return RubricScore(rubric=rubric, score=score, passed=False, reasoning=reasoning)


def _pass(rubric: RubricName, reasoning: str, score: float = 1.0) -> RubricScore:
    return RubricScore(
        rubric=rubric,
        score=score,
        passed=score >= RUBRIC_THRESHOLDS[rubric] - _EPS,
        reasoning=reasoning,
    )


def _skip(rubric: RubricName, reasoning: str) -> RubricScore:
    """A not-applicable result: excluded from means/pass-rates downstream."""
    return RubricScore(
        rubric=rubric, score=0.0, passed=True, reasoning=reasoning, skipped=True
    )


def _no_estimate(rubric: RubricName, sample: AgentSample) -> RubricScore | None:
    """Common guard for the twin rubrics: fail when there's no usable estimate."""
    obj = sample.output_obj
    if sample.error or obj is None:
        return _fail(rubric, f"No structured output to validate (error={sample.error}).")
    if not isinstance(obj, PhaseEstimate):
        return _fail(rubric, f"Output is not a PhaseEstimate (got {type(obj).__name__}).")
    if sample.is_stub:
        return _fail(rubric, "Output is the deterministic stub fallback (twin LLM call failed).")
    return None


def _roster_from_ctx(sample: AgentSample) -> RoleRoster:
    raw = sample.eval_context.get("roster")
    if isinstance(raw, RoleRoster):
        return raw
    if isinstance(raw, dict):
        return RoleRoster.model_validate(raw)
    return RoleRoster.default()


def _phase_from_ctx(sample: AgentSample, obj: PhaseEstimate) -> Phase:
    raw = sample.eval_context.get("phase")
    if isinstance(raw, Phase):
        return raw
    if isinstance(raw, str):
        return Phase(raw)
    return obj.phase


def _tooling_from_ctx(sample: AgentSample) -> AiToolingLevel:
    raw = sample.eval_context.get("tooling_level")
    if isinstance(raw, AiToolingLevel):
        return raw
    if isinstance(raw, str) and raw:
        return AiToolingLevel(raw)
    return AiToolingLevel.NONE


# --------------------------------------------------------------------------- #
# json_correctness (deterministic, twins)
# --------------------------------------------------------------------------- #


def _score_json_correctness(sample: AgentSample) -> RubricScore:
    """Deterministic structural check on a twin's structured output.

    Score 1.0 iff the output (a) round-trips through its Pydantic schema, (b) is
    NOT the deterministic stub fallback, and (c) passes its model invariants
    (non-empty role-hours lists). Otherwise <1.0 with a reason. No LLM call.
    """
    rubric: RubricName = "json_correctness"
    obj = sample.output_obj
    if sample.error or obj is None:
        return _fail(rubric, f"No structured output to validate (error={sample.error}).")
    if sample.is_stub:
        return _fail(rubric, "Output is the deterministic stub fallback (twin LLM call failed).")

    try:
        dumped = obj.model_dump(mode="json")
        type(obj).model_validate(dumped)
    except Exception as exc:  # noqa: BLE001
        return _fail(rubric, f"Pydantic round-trip failed: {exc}")

    reasons: list[str] = []
    ai_roles = getattr(obj, "ai_assisted_role_hours", None)
    manual_roles = getattr(obj, "manual_only_role_hours", None)
    if ai_roles is not None and not ai_roles:
        reasons.append("ai_assisted_role_hours is empty")
    if manual_roles is not None and not manual_roles:
        reasons.append("manual_only_role_hours is empty")
    if reasons:
        return _fail(rubric, "Model invariants violated: " + "; ".join(reasons), score=0.5)

    return _pass(rubric, "Valid: round-trips, not a stub, role-hours present.")


# --------------------------------------------------------------------------- #
# band_adherence (deterministic, twins) — the single highest-value check.
# --------------------------------------------------------------------------- #


def _score_band_adherence(sample: AgentSample) -> RubricScore:
    """Recompute the (lo, hi) reduction band from ai_acceleration and assert the
    twin's effective reduction sits inside it, plus dual-scenario sign consistency.
    """
    rubric: RubricName = "band_adherence"
    guard = _no_estimate(rubric, sample)
    if guard is not None:
        return guard
    obj: PhaseEstimate = sample.output_obj

    phase = _phase_from_ctx(sample, obj)
    tooling = _tooling_from_ctx(sample)
    bands = sample.eval_context.get("reduction_bands") or {}
    lo, hi = band_for(phase, tooling, bands)
    r = obj.effective_ai_reduction_pct / 100.0

    if hi <= 0.0:
        # No band for this (phase, tooling) — reduction MUST be zero (NONE tooling,
        # or AUTOCOMPLETE on discovery/ux/code_review).
        if abs(r) > _EPS:
            return _fail(
                rubric,
                f"phase={phase.value} tooling={tooling.value} has no band (hi=0) but "
                f"effective_ai_reduction_pct={obj.effective_ai_reduction_pct} (must be 0).",
            )
    elif not (NEGATIVE_FLOOR - _EPS <= r <= hi + _EPS):
        return _fail(
            rubric,
            f"effective_ai_reduction_pct={obj.effective_ai_reduction_pct} (r={r:.4f}) outside "
            f"[{NEGATIVE_FLOOR}, {hi}] for phase={phase.value} tooling={tooling.value}.",
        )

    # Dual-scenario sign consistency between the most-likely hour points.
    ai_ml = obj.ai_assisted_hours.most_likely
    manual_ml = obj.manual_only_hours.most_likely
    if r > _EPS:
        if manual_ml < ai_ml - _EPS:
            return _fail(
                rubric,
                f"reduction r={r:.4f}>0 but manual_ml={manual_ml} < ai_ml={ai_ml} "
                "(AI-assisted should be the lighter scenario).",
            )
    elif r < -_EPS:
        if ai_ml < manual_ml - _EPS:
            return _fail(
                rubric,
                f"reduction r={r:.4f}<0 but ai_ml={ai_ml} < manual_ml={manual_ml} "
                "(AI net-slower should make AI the heavier scenario).",
            )
    else:
        if abs(ai_ml - manual_ml) > max(1.0, 0.02 * max(manual_ml, 1.0)):
            return _fail(
                rubric,
                f"reduction r≈0 but ai_ml={ai_ml} differs from manual_ml={manual_ml}.",
            )

    return _pass(
        rubric,
        f"reduction {obj.effective_ai_reduction_pct}% within band "
        f"[{NEGATIVE_FLOOR}, {hi}] for phase={phase.value} tooling={tooling.value}; "
        "dual-scenario signs consistent.",
    )


# --------------------------------------------------------------------------- #
# algorithm_conformance (deterministic, twins)
# --------------------------------------------------------------------------- #


def _score_algorithm_conformance(sample: AgentSample) -> RubricScore:
    """ai_assisted ≈ manual_only × (1 − r) at each PERT point; breakdown finite & ≥0;
    PERT ordering on both ranges. Score = fraction of checks passing."""
    rubric: RubricName = "algorithm_conformance"
    guard = _no_estimate(rubric, sample)
    if guard is not None:
        return guard
    obj: PhaseEstimate = sample.output_obj

    r = obj.effective_ai_reduction_pct / 100.0
    checks: list[bool] = []
    failures: list[str] = []

    ai = obj.ai_assisted_hours
    manual = obj.manual_only_hours
    for label, a, m in (
        ("optimistic", ai.optimistic, manual.optimistic),
        ("most_likely", ai.most_likely, manual.most_likely),
        ("pessimistic", ai.pessimistic, manual.pessimistic),
    ):
        expected = m * (1.0 - r)
        tol = max(1.0, 0.02 * abs(expected))
        ok = abs(a - expected) <= tol
        checks.append(ok)
        if not ok:
            failures.append(
                f"{label}: ai={a} != manual×(1-r)={expected:.2f} (tol {tol:.2f})"
            )

    # PERT ordering on both ranges.
    for name, rng in (("ai_assisted", ai), ("manual_only", manual)):
        ok = rng.optimistic <= rng.most_likely + _EPS <= rng.pessimistic + _EPS and (
            rng.optimistic <= rng.pessimistic + _EPS
        )
        checks.append(ok)
        if not ok:
            failures.append(
                f"{name} PERT ordering violated: "
                f"o={rng.optimistic} m={rng.most_likely} p={rng.pessimistic}"
            )

    # Breakdown values finite and >= 0.
    for key, val in obj.breakdown.items():
        ok = math.isfinite(val) and val >= -_EPS
        checks.append(ok)
        if not ok:
            failures.append(f"breakdown[{key}]={val} not finite/non-negative")

    score = sum(1.0 for c in checks if c) / len(checks) if checks else 1.0
    if failures:
        return RubricScore(
            rubric=rubric,
            score=score,
            passed=score >= RUBRIC_THRESHOLDS[rubric] - _EPS,
            reasoning="Algorithm-conformance failures: " + "; ".join(failures),
        )
    return _pass(rubric, "All PERT points satisfy ai≈manual×(1-r); ordering + breakdown valid.")


# --------------------------------------------------------------------------- #
# role_attribution_validity (deterministic, twins)
# --------------------------------------------------------------------------- #


def _role_shares(roles: list[RoleHours]) -> tuple[float, dict[str, float]]:
    total = sum(rh.hours for rh in roles)
    if total <= 0:
        return 0.0, {rh.role_id: 0.0 for rh in roles}
    return total, {rh.role_id: rh.hours / total for rh in roles}


def _score_role_attribution_validity(sample: AgentSample) -> RubricScore:
    """role-hours sum to the scenario total (±1%); role_ids ⊆ roster; phase
    caps/floors honored (computed from manual_only_role_hours shares)."""
    rubric: RubricName = "role_attribution_validity"
    guard = _no_estimate(rubric, sample)
    if guard is not None:
        return guard
    obj: PhaseEstimate = sample.output_obj
    roster = _roster_from_ctx(sample)
    phase = _phase_from_ctx(sample, obj)
    roster_ids = {r.role_id for r in roster.roles}
    failures: list[str] = []

    # 1. Sums match the scenario most_likely total (±1%).
    for name, roles, target in (
        ("ai_assisted", obj.ai_assisted_role_hours, obj.ai_assisted_hours.most_likely),
        ("manual_only", obj.manual_only_role_hours, obj.manual_only_hours.most_likely),
    ):
        s = sum(rh.hours for rh in roles)
        tol = max(1.0, 0.01 * max(target, 1.0))
        if abs(s - target) > tol:
            failures.append(
                f"{name}_role_hours sum={s:.2f} != {name}_hours.most_likely={target} (tol {tol:.2f})"
            )
        for rh in roles:
            if rh.role_id not in roster_ids:
                failures.append(f"{name} role_id={rh.role_id!r} not in roster {sorted(roster_ids)}")

    # 2. Phase caps/floors, computed from manual_only_role_hours shares.
    _total, shares = _role_shares(obj.manual_only_role_hours)
    seniority = {r.role_id: r.seniority for r in roster.roles}
    category = {r.role_id: r.category for r in roster.roles}

    def junior_cap(cap: float, label: str) -> None:
        for rid, share in shares.items():
            if seniority.get(rid) == RoleSeniority.JUNIOR and share > cap + _EPS:
                failures.append(
                    f"{label}: junior role {rid!r} share={share:.3f} exceeds cap {cap}"
                )

    def category_floor(cats: set[RoleCategory], floor: float, label: str) -> None:
        got = sum(s for rid, s in shares.items() if category.get(rid) in cats)
        if got < floor - _EPS:
            failures.append(
                f"{label}: {'+'.join(c.value for c in cats)} share={got:.3f} below floor {floor}"
            )

    if phase is Phase.CODE_REVIEW:
        junior_cap(0.15, "CODE_REVIEW")
    elif phase is Phase.DISCOVERY:
        junior_cap(0.25, "DISCOVERY")
    elif phase is Phase.UX_DESIGN:
        category_floor({RoleCategory.PRODUCT, RoleCategory.UI_UX}, 0.40, "UX_DESIGN")
    elif phase is Phase.DEPLOYMENT:
        category_floor(
            {RoleCategory.ENGINEERING, RoleCategory.DEVOPS, RoleCategory.DATA},
            0.75,
            "DEPLOYMENT",
        )
    # DEVELOPMENT / QA_TESTING: no cap.

    if failures:
        return _fail(rubric, "Role-attribution failures: " + "; ".join(failures))
    return _pass(
        rubric,
        f"phase={phase.value}: role-hour sums match totals, role_ids in roster, "
        "phase caps/floors honored.",
    )


# --------------------------------------------------------------------------- #
# estimate_accuracy (deterministic, reference-based, twins)
# --------------------------------------------------------------------------- #


def _banded_rel_err_score(actual: float, target: float) -> float:
    """1.0 at rel_err<=0.25, 0.0 at >=0.60, linear between."""
    if target <= 0:
        return 1.0 if abs(actual) <= _EPS else 0.0
    rel = abs(actual - target) / target
    if rel <= 0.25:
        return 1.0
    if rel >= 0.60:
        return 0.0
    return (0.60 - rel) / (0.60 - 0.25)


def _score_estimate_accuracy(sample: AgentSample) -> RubricScore:
    """Banded relative error of the twin's most-likely hours vs the worked-example
    targets in ``gold``. SKIPS when the case carries no targets."""
    rubric: RubricName = "estimate_accuracy"
    gold = sample.gold or {}
    target_manual = gold.get("target_manual_ml")
    target_ai = gold.get("target_ai_ml")
    if target_manual is None and target_ai is None:
        return _skip(rubric, "Case carries no accuracy targets; skipped.")

    guard = _no_estimate(rubric, sample)
    if guard is not None:
        return guard
    obj: PhaseEstimate = sample.output_obj

    parts: list[float] = []
    detail: list[str] = []
    if target_manual is not None:
        actual = obj.manual_only_hours.most_likely
        s = _banded_rel_err_score(actual, float(target_manual))
        parts.append(s)
        detail.append(f"manual_ml actual={actual} target={target_manual} -> {s:.2f}")
    if target_ai is not None:
        actual = obj.ai_assisted_hours.most_likely
        s = _banded_rel_err_score(actual, float(target_ai))
        parts.append(s)
        detail.append(f"ai_ml actual={actual} target={target_ai} -> {s:.2f}")

    score = sum(parts) / len(parts)
    return RubricScore(
        rubric=rubric,
        score=score,
        passed=score >= RUBRIC_THRESHOLDS[rubric] - _EPS,
        reasoning="Banded accuracy vs targets: " + "; ".join(detail),
    )


# --------------------------------------------------------------------------- #
# extraction_accuracy (deterministic, prefill)
# --------------------------------------------------------------------------- #


def _within_tol(actual: int | None, target: int | None) -> bool:
    """±1 or ±20%, treating None/0 as 0."""
    a = actual or 0
    t = target or 0
    return abs(a - t) <= max(1, round(0.20 * t))


def _score_extraction_accuracy(sample: AgentSample) -> RubricScore:
    """Compare prefill's NormalizedProjectContext to gold field-by-field."""
    rubric: RubricName = "extraction_accuracy"
    gold = sample.gold or {}
    obj = sample.output_obj
    if sample.error or obj is None:
        return _fail(rubric, f"No structured output to validate (error={sample.error}).")
    if not gold:
        return _skip(rubric, "Case carries no extraction gold; skipped.")

    industry = getattr(getattr(obj, "industry", None), "value", getattr(obj, "industry", None))
    project_type = getattr(
        getattr(obj, "project_type", None), "value", getattr(obj, "project_type", None)
    )
    regs = {
        getattr(r, "value", r) for r in getattr(obj, "regulatory_requirements", []) or []
    }
    screens = getattr(obj, "screen_count_estimate", None)
    integrations = getattr(obj, "integrations", []) or []

    checks: list[bool] = []
    detail: list[str] = []

    def record(name: str, ok: bool, got: object, want: object) -> None:
        checks.append(ok)
        detail.append(f"{name}: got={got!r} want={want!r} {'ok' if ok else 'MISMATCH'}")

    record("industry", industry == gold.get("industry"), industry, gold.get("industry"))
    record(
        "project_type",
        project_type == gold.get("project_type"),
        project_type,
        gold.get("project_type"),
    )
    want_regs = set(gold.get("regulatory_requirements", []))
    record("regulatory", regs == want_regs, sorted(regs), sorted(want_regs))
    record(
        "screen_count",
        _within_tol(screens, gold.get("screen_count")),
        screens,
        gold.get("screen_count"),
    )
    record(
        "integration_count",
        _within_tol(len(integrations), gold.get("integration_count")),
        len(integrations),
        gold.get("integration_count"),
    )

    score = sum(1.0 for c in checks if c) / len(checks)
    if score < 1.0 - _EPS:
        return _fail(rubric, "Extraction mismatches: " + "; ".join(detail), score=score)
    return _pass(rubric, "All extracted fields match gold: " + "; ".join(detail))


# --------------------------------------------------------------------------- #
# staffing_adequacy (deterministic, roster)
# --------------------------------------------------------------------------- #


def _score_staffing_adequacy(sample: AgentSample) -> RubricScore:
    """Required categories present + no single role > 60%. Requirements derived
    from the case's Stage 2 signals."""
    rubric: RubricName = "staffing_adequacy"
    obj = sample.output_obj
    if sample.error or obj is None:
        return _fail(rubric, f"No structured output to validate (error={sample.error}).")

    roles = getattr(obj, "roles", None)
    if not roles:
        return _fail(rubric, "Roster proposal has no roles.")

    signals = sample.eval_context.get("stage2_signals") or {}
    required = {RoleCategory.ENGINEERING, RoleCategory.PRODUCT}
    if (signals.get("screen_count") or 0) > 0:
        required.add(RoleCategory.UI_UX)
    if signals.get("regulatory"):
        required.add(RoleCategory.QA)

    present = {getattr(r.category, "value", r.category) for r in roles}
    present_cats = {RoleCategory(c) if not isinstance(c, RoleCategory) else c for c in present}
    missing = sorted(c.value for c in required if c not in present_cats)
    covered = len(required) - len(missing)
    score = covered / len(required) if required else 1.0

    # Hard fail if any single role exceeds 60% of total effort.
    pcts = [getattr(r, "percentage", 0.0) for r in roles]
    over = [p for p in pcts if p > 60.0 + _EPS]
    if over:
        return _fail(
            rubric,
            f"A role holds {max(over):.0f}% (>60% concentration); required categories "
            f"covered={covered}/{len(required)}.",
            score=0.0,
        )
    if missing:
        return _fail(
            rubric,
            f"Missing required categories {missing}; covered {covered}/{len(required)}.",
            score=score,
        )
    return _pass(rubric, f"All required categories {sorted(c.value for c in required)} present; "
                 "no role over 60%.")


# --------------------------------------------------------------------------- #
# classification_accuracy + enum_constraint_adherence (deterministic, tooling)
# --------------------------------------------------------------------------- #

_TOOLING_PHASES = ("discovery", "ux_design", "development", "code_review", "deployment", "qa_testing")
# Phases that have NO autocomplete band — autocomplete is a code-writing assist.
_NO_AUTOCOMPLETE_PHASES = ("discovery", "ux_design", "code_review")


def _score_classification_accuracy(sample: AgentSample) -> RubricScore:
    """Per-phase exact match of PhaseToolingLevels vs gold across all 6 phases."""
    rubric: RubricName = "classification_accuracy"
    obj = sample.output_obj
    if sample.error or obj is None:
        return _fail(rubric, f"No structured output to validate (error={sample.error}).")
    gold = (sample.gold or {}).get("ai_tooling")
    if not gold:
        return _skip(rubric, "Case carries no tooling gold; skipped.")

    levels = getattr(obj, "ai_tooling", None)
    if levels is None:
        return _fail(rubric, "Output has no ai_tooling field.")

    matches = 0
    detail: list[str] = []
    for phase in _TOOLING_PHASES:
        got = getattr(getattr(levels, phase), "value", getattr(levels, phase))
        want = gold.get(phase, "none")
        ok = got == want
        matches += 1 if ok else 0
        detail.append(f"{phase}: got={got} want={want} {'ok' if ok else 'X'}")

    score = matches / len(_TOOLING_PHASES)
    if score < 1.0 - _EPS:
        return _fail(rubric, "Tooling label mismatches: " + "; ".join(detail), score=score)
    return _pass(rubric, "All 6 phase labels match gold.")


def _score_enum_constraint_adherence(sample: AgentSample) -> RubricScore:
    """No AUTOCOMPLETE on discovery/ux_design/code_review; all levels valid enums."""
    rubric: RubricName = "enum_constraint_adherence"
    obj = sample.output_obj
    if sample.error or obj is None:
        return _fail(rubric, f"No structured output to validate (error={sample.error}).")
    levels = getattr(obj, "ai_tooling", None)
    if levels is None:
        return _fail(rubric, "Output has no ai_tooling field.")

    valid = {lvl.value for lvl in AiToolingLevel}
    failures: list[str] = []
    for phase in _TOOLING_PHASES:
        raw = getattr(levels, phase)
        val = getattr(raw, "value", raw)
        if val not in valid:
            failures.append(f"{phase}={val!r} is not a valid AiToolingLevel")
        elif phase in _NO_AUTOCOMPLETE_PHASES and val == AiToolingLevel.AUTOCOMPLETE.value:
            failures.append(f"{phase}=autocomplete but that phase has no autocomplete band")

    if failures:
        return _fail(rubric, "Enum-constraint violations: " + "; ".join(failures))
    return _pass(rubric, "All levels valid; no autocomplete on discovery/ux_design/code_review.")


# --------------------------------------------------------------------------- #
# partition_correctness (deterministic, consolidator)
# --------------------------------------------------------------------------- #


def _score_partition_correctness(sample: AgentSample) -> RubricScore:
    """Proxy partition scoring (the consolidator's output does not expose the
    cluster→input-index mapping — see report): (a) no input topic/question dropped
    from the merged set's coverage, and (b) output cluster count == gold count."""
    rubric: RubricName = "partition_correctness"
    obj = sample.output_obj
    if sample.error or obj is None:
        return _fail(rubric, f"No structured output to validate (error={sample.error}).")

    # obj is list[tuple[Gap, list[Phase]]] — the merged questions.
    merged = list(obj)
    out_count = len(merged)

    gold = sample.gold or {}
    clusters = gold.get("clusters")
    if clusters is None:
        return _skip(rubric, "Case carries no gold clustering; skipped.")
    gold_count = len(clusters)

    # Coverage: every input phase represented in the merged output's phase-union.
    in_phases: set[str] = set()
    for phase_list in sample.eval_context.get("input_phases", []):
        in_phases.update(phase_list)
    out_phases: set[str] = set()
    for _gap, phases in merged:
        out_phases.update(p.value if hasattr(p, "value") else str(p) for p in phases)

    failures: list[str] = []
    if out_count != gold_count:
        failures.append(f"output cluster count={out_count} != gold count={gold_count}")
    dropped = in_phases - out_phases
    if dropped:
        failures.append(f"input phases dropped from coverage: {sorted(dropped)}")

    if failures:
        return _fail(rubric, "Partition failures: " + "; ".join(failures))
    return _pass(
        rubric,
        f"output clusters={out_count} == gold={gold_count}; phase coverage preserved.",
    )


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

_DETERMINISTIC = {
    "json_correctness": _score_json_correctness,
    "band_adherence": _score_band_adherence,
    "algorithm_conformance": _score_algorithm_conformance,
    "role_attribution_validity": _score_role_attribution_validity,
    "estimate_accuracy": _score_estimate_accuracy,
    "extraction_accuracy": _score_extraction_accuracy,
    "staffing_adequacy": _score_staffing_adequacy,
    "classification_accuracy": _score_classification_accuracy,
    "enum_constraint_adherence": _score_enum_constraint_adherence,
    "partition_correctness": _score_partition_correctness,
}


async def score(
    rubric: RubricName, sample: AgentSample, *, judge_model: str
) -> RubricScore:
    """Score one sample against one rubric. Never raises.

    The deterministic rubrics run synchronously (no LLM); the three judge rubrics
    call ``call_structured``. Any judge exception is captured into the RubricScore.
    """
    fn = _DETERMINISTIC.get(rubric)
    if fn is not None:
        try:
            return fn(sample)
        except Exception as exc:  # noqa: BLE001
            logger.warning("deterministic rubric=%s case=%s raised: %s", rubric, sample.case_id, exc)
            return RubricScore(
                rubric=rubric, score=0.0, passed=False, reasoning="", error=str(exc)
            )

    threshold = RUBRIC_THRESHOLDS[rubric]
    prompt = _PROMPTS[rubric]
    try:
        verdict = await call_structured(
            system=prompt,
            user=_build_user_message(rubric, sample),
            response_model=_Verdict,
            tool_name="submit_evaluation",
            model=judge_model,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("judge failed for rubric=%s case=%s: %s", rubric, sample.case_id, exc)
        return RubricScore(
            rubric=rubric, score=0.0, passed=False, reasoning="", error=str(exc)
        )

    return RubricScore(
        rubric=rubric,
        score=verdict.score,
        passed=verdict.score >= threshold,
        reasoning=verdict.reasoning,
    )

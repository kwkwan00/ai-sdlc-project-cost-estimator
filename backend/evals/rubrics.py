"""Rubric scoring — deterministic correctness checks + a few LLM-as-judge rubrics.

The high-value checks here are DETERMINISTIC (no LLM): they recompute the project's
own oracles (``orchestrator.ai_acceleration.band_for`` for the AI-reduction bands,
``orchestrator.role_attribution`` for the phase caps/floors, the PERT identity for
the dual-scenario hours) and assert the agent's output conforms. Each returns a
``RubricScore`` naming the offending value in ``reasoning``.

Three rubrics remain LLM-judged (``faithfulness``, ``plan_quality``,
``summarization``); they go through ``evals.judge.judge_structured``, which defaults
to OpenAI GPT-5.5 (a different provider from the Anthropic twins it grades) and parses
into a ``_Verdict`` response model. Pointing ``--judge-model`` at a ``claude-*`` model
transparently falls back to ``orchestrator.llm.call_structured``.

``score(...)`` dispatches by rubric name and never raises: a judge failure is
captured as a ``RubricScore`` with ``error`` set and ``score=0.0`` so one bad
sample can't abort the batch.
"""

from __future__ import annotations

import logging
import math
import statistics

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

from .judge import judge_structured
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
You are a strict evaluator of an AI ESTIMATION agent's FAITHFULNESS to its grounding.

You are given:
- TASK INPUT: the task the agent was asked to perform.
- GROUNDING CONTEXT: the project description + structured context the agent was given.
- ACTUAL OUTPUT: what the agent produced.

Score, in [0, 1], how well the output's claims and assumptions are SUPPORTED by — or at
least CONSISTENT with — the grounding context. Penalize genuine HALLUCINATION: scope the
description does not imply (invented integrations, features, user roles, or regulatory
regimes), quantities that CONTRADICT the description (e.g. a different screen or integration
count than stated), or claims presented as established fact that the context does not
support. A high score means the output stays within the described scope; a low score means
it invents or contradicts facts.

NOTE on ALGORITHM INTERNALS (do NOT penalize): the agent runs a FORMAL estimation algorithm
(UCP / SCP / COCOMO II / Fagan / CMP / TPA). Its computed numeric internals — SLOC/FP/KLOC
conversions, scale factors, EAF, inspection & rework rates, test points, hour ranges
(optimistic/most-likely/pessimistic), confidence values, and component scores — are DERIVED
ALGORITHM OUTPUTS, not factual claims about the project. They will not appear verbatim in
the description and need NOT be "grounded" in it. Do not treat them as fabrications; only
flag a number if it CONTRADICTS a quantity the context explicitly states.

NOTE on PROPOSED SIZING ASSUMPTIONS (do NOT penalize when reasonable): estimating an
under-specified project REQUIRES the agent to propose values for missing drivers (e.g. a
likely tech stack, an assumed screen count when none is given, standard controls for the
stated regulatory regime). When such a value is REASONABLE for the described scope and is
framed as an assumption / risk / gap / clarifying question, it is EXPECTED estimation
behavior, not a hallucination. Only penalize an assumption that CONTRADICTS the context or
expands scope the description does not imply.

NOTE on the AI reduction guardrail: a twin's `effective_ai_reduction_pct` is a
POST-SCALING system value. The proposed reduction is clamped into the guardrail band,
then the system scales it down by codebase context and team seniority, so the effective
value may legitimately fall BELOW the band's `min_pct` (even go negative). Do NOT treat
`effective_ai_reduction_pct` being below the guardrail minimum as a violation,
contradiction, or unfaithful claim — that is expected, correct behavior.

Steps:
1. Enumerate the output's material claims and assumptions. Set aside derived algorithm
   internals and `effective_ai_reduction_pct` per the NOTES above — those are not judged
   for grounding.
2. For each remaining claim/assumption, mark it SUPPORTED (stated or reasonably implied by
   the context, or a reasonable framed assumption) or UNSUPPORTED (contradicts the context
   or invents scope it does not imply).
3. Score by the proportion supported, weighting genuine scope inventions and contradictions
   heavily and reasonable framed assumptions not at all.

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
    """Conformance checks for the Monte-Carlo-derived dual-scenario ranges:

    (i)   ``ai.most_likely ≈ manual.most_likely × (1 − r)`` — the DETERMINISTIC-mid
          identity still holds exactly (the MC layer only widens the band, it does
          not move the modal draw);
    (ii)  PERT ordering on both ranges;
    (iii) ``breakdown`` values finite & ≥ 0;
    (iv)  ``ai.pXX ≤ manual.pXX`` (within eps) at optimistic/most_likely/pessimistic
          when ``r ≥ 0`` — per-draw ``ai ≤ manual`` for non-negative reduction, so it
          must survive at every reported percentile (sign consistency).

    The OLD per-percentile ``ai == manual×(1-r)`` equality at optimistic/pessimistic
    is intentionally DROPPED: risk + reduction sampling add variance that the simple
    comonotonic identity cannot model, so it is no longer a valid invariant.
    Score = fraction of checks passing.
    """
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

    # (i) Deterministic-mid identity — STILL exact.
    expected_ml = manual.most_likely * (1.0 - r)
    tol_ml = max(1.0, 0.02 * abs(expected_ml))
    ok = abs(ai.most_likely - expected_ml) <= tol_ml
    checks.append(ok)
    if not ok:
        failures.append(
            f"most_likely: ai={ai.most_likely} != manual×(1-r)={expected_ml:.2f} (tol {tol_ml:.2f})"
        )

    # (ii) PERT ordering on both ranges.
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

    # (iii) Breakdown values finite and >= 0.
    for key, val in obj.breakdown.items():
        ok = math.isfinite(val) and val >= -_EPS
        checks.append(ok)
        if not ok:
            failures.append(f"breakdown[{key}]={val} not finite/non-negative")

    # (iv) Sign consistency: for r >= 0, ai <= manual at each reported percentile.
    if r >= -_EPS:
        for label, a, m in (
            ("optimistic", ai.optimistic, manual.optimistic),
            ("most_likely", ai.most_likely, manual.most_likely),
            ("pessimistic", ai.pessimistic, manual.pessimistic),
        ):
            tol = max(1.0, 0.02 * abs(m))
            ok = a <= m + tol
            checks.append(ok)
            if not ok:
                failures.append(
                    f"{label}: ai={a} > manual={m} but reduction r={r:.4f}>=0 "
                    f"(AI should not exceed manual; tol {tol:.2f})"
                )

    score = sum(1.0 for c in checks if c) / len(checks) if checks else 1.0
    if failures:
        return RubricScore(
            rubric=rubric,
            score=score,
            passed=score >= RUBRIC_THRESHOLDS[rubric] - _EPS,
            reasoning="Algorithm-conformance failures: " + "; ".join(failures),
        )
    return _pass(
        rubric,
        "ai.most_likely≈manual×(1-r); PERT ordering + breakdown valid; "
        "ai≤manual at every percentile.",
    )


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
# interval_calibration (deterministic, reference-based, twins)
# --------------------------------------------------------------------------- #


def _interval_hit_score(actual: float, lo: float, hi: float) -> float:
    """1.0 when ``actual`` falls in ``[lo, hi]``; otherwise banded partial credit by
    relative distance to the NEARER bound (reuses ``_banded_rel_err_score`` so the
    bands match ``estimate_accuracy``: full credit ≤0.25 rel, zero ≥0.60)."""
    if lo - _EPS <= actual <= hi + _EPS:
        return 1.0
    nearer = lo if abs(actual - lo) <= abs(actual - hi) else hi
    return _banded_rel_err_score(actual, nearer)


def _score_interval_calibration(sample: AgentSample) -> RubricScore:
    """Does the realized actual land inside the predicted ``[optimistic, pessimistic]``
    band? Reference-based: reads ``gold["actual_manual_ml"]`` / ``actual_ai_ml`` and
    checks each against its scenario's band, with banded partial credit by relative
    distance to the nearer bound. SKIPS when the case carries no such gold."""
    rubric: RubricName = "interval_calibration"
    gold = sample.gold or {}
    actual_manual = gold.get("actual_manual_ml")
    actual_ai = gold.get("actual_ai_ml")
    if actual_manual is None and actual_ai is None:
        return _skip(rubric, "Case carries no interval actuals; skipped.")

    guard = _no_estimate(rubric, sample)
    if guard is not None:
        return guard
    obj: PhaseEstimate = sample.output_obj

    parts: list[float] = []
    detail: list[str] = []
    if actual_manual is not None:
        rng = obj.manual_only_hours
        s = _interval_hit_score(float(actual_manual), rng.optimistic, rng.pessimistic)
        parts.append(s)
        detail.append(
            f"manual actual={actual_manual} in [{rng.optimistic}, {rng.pessimistic}] -> {s:.2f}"
        )
    if actual_ai is not None:
        rng = obj.ai_assisted_hours
        s = _interval_hit_score(float(actual_ai), rng.optimistic, rng.pessimistic)
        parts.append(s)
        detail.append(
            f"ai actual={actual_ai} in [{rng.optimistic}, {rng.pessimistic}] -> {s:.2f}"
        )

    score = sum(parts) / len(parts)
    return RubricScore(
        rubric=rubric,
        score=score,
        passed=score >= RUBRIC_THRESHOLDS[rubric] - _EPS,
        reasoning="Interval calibration vs actuals: " + "; ".join(detail),
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


def _score_roster_catalog_selection(sample: AgentSample) -> RubricScore:
    """When the case supplies an org catalog and a gold ``expected_catalog_role_id``, assert the
    agent actually SELECTED that predefined role (set some role's ``catalog_role_id`` to it). Skips
    when there's no gold target or no LLM output (e.g. CI without an API key) — it gates the
    deliberate-selection behavior only when there's something to check."""
    rubric: RubricName = "roster_catalog_selection"
    gold_id = sample.gold.get("expected_catalog_role_id")
    if not gold_id:
        return _skip(rubric, "No expected_catalog_role_id in gold.")
    obj = sample.output_obj
    if sample.error or obj is None:
        return _skip(rubric, f"No roster output to check (error={sample.error}).")
    roles = getattr(obj, "roles", None) or []
    selected = {getattr(r, "catalog_role_id", None) for r in roles}
    if gold_id in selected:
        return _pass(rubric, f"Agent selected catalog role {gold_id!r}.")
    chosen = sorted(s for s in selected if s)
    return _fail(rubric, f"Agent did not select catalog role {gold_id!r}; selections={chosen}.")


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


def _pairs(cluster: list[int]) -> set[frozenset[int]]:
    """The set of co-membership pairs within one cluster (singletons contribute none)."""
    members = sorted(set(cluster))
    out: set[frozenset[int]] = set()
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            out.add(frozenset((members[i], members[j])))
    return out


def _pairwise_f1(predicted: list[list[int]], gold: list[list[int]]) -> float:
    """Pairwise-F1 agreement between two clusterings of the same index universe.

    Treats clustering as the set of same-cluster index PAIRS. F1 of the predicted
    pair-set vs the gold pair-set: a perfect partition scores 1.0; a *lost* question
    (a gold pair split apart) drops recall; a *spurious* merge (a pair joined that
    gold keeps apart) drops precision. The all-singletons edge case (no pairs on
    either side) is defined as 1.0 (the two clusterings agree: nothing is merged)."""
    pred_pairs: set[frozenset[int]] = set()
    for c in predicted:
        pred_pairs |= _pairs(c)
    gold_pairs: set[frozenset[int]] = set()
    for c in gold:
        gold_pairs |= _pairs(c)

    if not pred_pairs and not gold_pairs:
        return 1.0  # both all-singletons: they agree (nothing merged)
    tp = len(pred_pairs & gold_pairs)
    fp = len(pred_pairs - gold_pairs)
    fn = len(gold_pairs - pred_pairs)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    if precision + recall <= _EPS:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _covers_universe(partition: list[list[int]], n: int) -> bool:
    """True iff the partition is an exact cover of indices 0..n-1 with no overlaps."""
    seen: list[int] = [idx for cluster in partition for idx in cluster]
    return sorted(seen) == list(range(n))


def _score_partition_correctness(sample: AgentSample) -> RubricScore:
    """Score the consolidator's predicted clustering against the gold clustering.

    EXACT mode (preferred): when the adapter recorded the cluster→input-index mapping
    in ``eval_context["predicted_partition"]`` (surfaced by
    ``merge_pass1._consolidate_with_partition``), score it against ``gold["clusters"]``
    by pairwise-F1 — a perfect partition is 1.0, a *lost* question (gold pair split)
    lowers recall, a *spurious* merge (non-gold pair joined) lowers precision. A
    predicted partition that is not an exact cover of the candidate universe is a hard
    fail (lost/duplicated indices).

    PROXY fallback: when no predicted partition is available, fall back to the original
    count + phase-coverage heuristic (output cluster count == gold count and no input
    phase dropped from the merged set's coverage)."""
    rubric: RubricName = "partition_correctness"
    obj = sample.output_obj
    if sample.error or obj is None:
        return _fail(rubric, f"No structured output to validate (error={sample.error}).")

    gold = sample.gold or {}
    clusters = gold.get("clusters")
    if clusters is None:
        return _skip(rubric, "Case carries no gold clustering; skipped.")
    gold_clusters: list[list[int]] = [list(c) for c in clusters]

    # obj is list[tuple[Gap, list[Phase]]] — the merged questions.
    merged = list(obj)

    predicted = sample.eval_context.get("predicted_partition")
    if isinstance(predicted, list) and predicted is not None:
        predicted_clusters: list[list[int]] = [list(c) for c in predicted]
        # Derive the candidate universe from the gold cover (authoritative).
        universe = sorted({idx for cluster in gold_clusters for idx in cluster})
        n = (max(universe) + 1) if universe else 0
        if not _covers_universe(predicted_clusters, n):
            return _fail(
                rubric,
                f"predicted partition {predicted_clusters} is not an exact cover of "
                f"0..{n - 1} (lost, duplicated, or out-of-range indices).",
            )
        score = _pairwise_f1(predicted_clusters, gold_clusters)
        return RubricScore(
            rubric=rubric,
            score=score,
            passed=score >= RUBRIC_THRESHOLDS[rubric] - _EPS,
            reasoning=(
                f"pairwise-F1={score:.3f} for predicted {predicted_clusters} vs gold "
                f"{gold_clusters} ({len(merged)} merged question(s))."
            ),
        )

    # ----- Proxy fallback (no predicted mapping available) ----- #
    out_count = len(merged)
    gold_count = len(gold_clusters)
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
        return _fail(rubric, "Partition failures (proxy): " + "; ".join(failures))
    return _pass(
        rubric,
        f"proxy: output clusters={out_count} == gold={gold_count}; phase coverage preserved.",
    )


# --------------------------------------------------------------------------- #
# wbs_structural (deterministic) — WBS planner
# --------------------------------------------------------------------------- #

_WBS_MIN_LEAVES = 3
_WBS_MIN_PHASES = 2


def _score_wbs_structural(sample: AgentSample) -> RubricScore:
    """Validate the planner's WBS tree is a usable, well-formed decomposition: enough leaf tasks
    spanning multiple phases, and every leaf carries a phase, a roster ``role_id``, and a positive,
    PERT-ordered 3-point estimate. Runs offline (the planner always yields a tree — the deterministic
    fallback when no API key), so it's a real CI gate on both the fallback and the live output."""
    rubric: RubricName = "wbs_structural"
    tree = sample.output_obj
    if sample.error or not tree:
        return _fail(rubric, f"No WBS tree to validate (error={sample.error}).")
    from models.wbs_task import iter_leaves

    leaves = list(iter_leaves(tree))
    if len(leaves) < _WBS_MIN_LEAVES:
        return _fail(rubric, f"Too few leaf tasks ({len(leaves)}); a usable WBS has ≥{_WBS_MIN_LEAVES}.")

    roster_ids = set(sample.eval_context.get("roster_role_ids") or [])
    problems: list[str] = []
    phases: set[str] = set()
    for leaf in leaves:
        if leaf.phase is None:
            problems.append(f"{leaf.id}: no phase")
        else:
            phases.add(leaf.phase.value)
        if roster_ids and leaf.role_id not in roster_ids:
            problems.append(f"{leaf.id}: role_id {leaf.role_id!r} not in roster")
        o, m, p = leaf.optimistic, leaf.most_likely, leaf.pessimistic
        if None in (o, m, p) or not (0 < o <= m <= p):  # type: ignore[operator]
            problems.append(f"{leaf.id}: non-positive / unordered hours {o}/{m}/{p}")
    if len(phases) < _WBS_MIN_PHASES:
        problems.append(f"only {len(phases)} distinct phase(s); expected ≥{_WBS_MIN_PHASES}")

    if problems:
        return _fail(rubric, "; ".join(problems[:5]))
    return _pass(
        rubric,
        f"{len(leaves)} leaves across {len(phases)} phases; all carry phase + roster role + "
        "ordered positive PERT.",
    )


# --------------------------------------------------------------------------- #
# consistency (deterministic, multi-sample) — twins + tooling
# --------------------------------------------------------------------------- #

# Coefficient-of-variation band for the twins' run-to-run stability. CoV <= LO is
# perfectly stable (1.0); CoV >= HI is fully unstable (0.0); linear between. A CoV of
# ~0.10 (10% swing between identical runs) still scores ~0.83 — tolerant of small
# Monte-Carlo / sampling jitter while catching genuinely flappy twins.
_CONSISTENCY_COV_LO = 0.02
_CONSISTENCY_COV_HI = 0.35


def _cov_score(values: list[float]) -> tuple[float, float]:
    """Return ``(score, cov)`` for a list of run-to-run numeric outputs. ``cov`` is the
    coefficient of variation (population std / mean); score is 1.0 at ``cov<=LO`` and
    0.0 at ``cov>=HI``, linear between. A near-zero mean with near-zero spread is
    treated as perfectly stable."""
    mean = statistics.fmean(values) if values else 0.0
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    if abs(mean) <= _EPS:
        return (1.0 if std <= _EPS else 0.0), 0.0
    cov = std / abs(mean)
    if cov <= _CONSISTENCY_COV_LO:
        return 1.0, cov
    if cov >= _CONSISTENCY_COV_HI:
        return 0.0, cov
    return (_CONSISTENCY_COV_HI - cov) / (_CONSISTENCY_COV_HI - _CONSISTENCY_COV_LO), cov


def _twin_consistency(samples: list[AgentSample]) -> RubricScore:
    """Run-to-run stability of a twin's key numeric output: the coefficient of
    variation of ``manual_only_hours.most_likely`` across the N samples."""
    rubric: RubricName = "consistency"
    values: list[float] = []
    for s in samples:
        obj = s.output_obj
        if s.error or obj is None or not isinstance(obj, PhaseEstimate) or s.is_stub:
            return _fail(
                rubric,
                f"A run produced no usable estimate (error={s.error}, stub={s.is_stub}); "
                "cannot assess consistency.",
            )
        values.append(obj.manual_only_hours.most_likely)

    score, cov = _cov_score(values)
    return RubricScore(
        rubric=rubric,
        score=score,
        passed=score >= RUBRIC_THRESHOLDS[rubric] - _EPS,
        reasoning=(
            f"manual_ml across {len(values)} run(s)={[round(v, 1) for v in values]} "
            f"CoV={cov:.3f} -> {score:.2f}."
        ),
    )


def _tooling_consistency(samples: list[AgentSample]) -> RubricScore:
    """Run-to-run per-phase label agreement for the tooling classifier: the fraction
    of phases on which ALL runs emit the same AiToolingLevel."""
    rubric: RubricName = "consistency"
    per_phase: dict[str, set[str]] = {p: set() for p in _TOOLING_PHASES}
    for s in samples:
        obj = s.output_obj
        levels = getattr(obj, "ai_tooling", None) if obj is not None else None
        if s.error or levels is None:
            return _fail(
                rubric,
                f"A run produced no ai_tooling labels (error={s.error}); "
                "cannot assess consistency.",
            )
        for phase in _TOOLING_PHASES:
            raw = getattr(levels, phase)
            per_phase[phase].add(str(getattr(raw, "value", raw)))

    agree = sum(1 for vals in per_phase.values() if len(vals) <= 1)
    score = agree / len(_TOOLING_PHASES)
    unstable = sorted(p for p, vals in per_phase.items() if len(vals) > 1)
    detail = f"{agree}/{len(_TOOLING_PHASES)} phases agree across {len(samples)} run(s)"
    if unstable:
        detail += f"; unstable: {unstable}"
    return RubricScore(
        rubric=rubric,
        score=score,
        passed=score >= RUBRIC_THRESHOLDS[rubric] - _EPS,
        reasoning=detail + ".",
    )


def _score_consistency(samples: list[AgentSample]) -> RubricScore:
    """Self-consistency over N adapter re-runs of the same case. SKIPS at N<=1 (nothing
    to compare). Dispatches twins → CoV of manual_ml; tooling → per-phase label
    agreement. Other agents are not registered for this rubric."""
    rubric: RubricName = "consistency"
    if not samples:
        return _fail(rubric, "No samples to assess consistency.")
    if len(samples) <= 1:
        return _skip(rubric, "Single run (repeats<=1); consistency not measured.")

    agent = samples[0].agent
    if agent == "tooling":
        return _tooling_consistency(samples)
    # Default: treat as a twin (the matrix only assigns consistency to twins + tooling).
    return _twin_consistency(samples)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

_DETERMINISTIC = {
    "json_correctness": _score_json_correctness,
    "band_adherence": _score_band_adherence,
    "algorithm_conformance": _score_algorithm_conformance,
    "role_attribution_validity": _score_role_attribution_validity,
    "estimate_accuracy": _score_estimate_accuracy,
    "interval_calibration": _score_interval_calibration,
    "extraction_accuracy": _score_extraction_accuracy,
    "staffing_adequacy": _score_staffing_adequacy,
    "roster_catalog_selection": _score_roster_catalog_selection,
    "classification_accuracy": _score_classification_accuracy,
    "enum_constraint_adherence": _score_enum_constraint_adherence,
    "partition_correctness": _score_partition_correctness,
    "wbs_structural": _score_wbs_structural,
}


async def _judge_one(rubric: RubricName, sample: AgentSample, *, judge_model: str) -> RubricScore:
    """One LLM-judge call for a judge rubric → RubricScore. Never raises."""
    threshold = RUBRIC_THRESHOLDS[rubric]
    prompt = _PROMPTS[rubric]
    try:
        verdict = await judge_structured(
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


async def score(
    rubric: RubricName, sample: AgentSample, *, judge_model: str
) -> RubricScore:
    """Score one sample against one rubric. Never raises.

    The deterministic rubrics run synchronously (no LLM); the three judge rubrics
    call ``judge_structured``. The multi-sample ``consistency`` rubric cannot be scored
    from a single sample, so it SKIPS here (the runner routes it through
    ``score_multi``). Any judge exception is captured into the RubricScore.
    """
    if rubric == "consistency":
        return _score_consistency([sample])

    fn = _DETERMINISTIC.get(rubric)
    if fn is not None:
        try:
            return fn(sample)
        except Exception as exc:  # noqa: BLE001
            logger.warning("deterministic rubric=%s case=%s raised: %s", rubric, sample.case_id, exc)
            return RubricScore(
                rubric=rubric, score=0.0, passed=False, reasoning="", error=str(exc)
            )

    return await _judge_one(rubric, sample, judge_model=judge_model)


async def score_multi(
    rubric: RubricName, samples: list[AgentSample], *, judge_model: str
) -> RubricScore:
    """Score a rubric that consumes MULTIPLE re-runs of the same case. Never raises.

    Used by the runner only for rubrics in ``models.NEEDS_MULTI_SAMPLE``:
    - ``consistency`` — deterministic run-to-run stability over the samples.
    - ``faithfulness`` — average the judge verdict over the samples to damp judge noise
      (one judge call per sample; the mean score is reported, ``passed`` against the
      threshold). Falls back to the single-sample behavior when ``len(samples)==1``.

    Any other rubric (or an empty list) falls back to single-sample ``score`` on the
    first sample, so this is always safe to call.
    """
    if not samples:
        return RubricScore(
            rubric=rubric, score=0.0, passed=False, reasoning="No samples to score."
        )
    if rubric == "consistency":
        return _score_consistency(samples)
    if rubric == "faithfulness":
        verdicts = [await _judge_one(rubric, s, judge_model=judge_model) for s in samples]
        scored = [v for v in verdicts if v.error is None]
        if not scored:
            return verdicts[0]  # propagate the (captured) judge error
        mean_score = statistics.fmean(v.score for v in scored)
        return RubricScore(
            rubric=rubric,
            score=mean_score,
            passed=mean_score >= RUBRIC_THRESHOLDS[rubric],
            reasoning=(
                f"faithfulness averaged over {len(scored)}/{len(samples)} run(s): "
                f"scores={[round(v.score, 3) for v in scored]} mean={mean_score:.3f}."
            ),
        )
    # Not a multi-sample rubric — score the first sample.
    return await score(rubric, samples[0], judge_model=judge_model)

"""Shared helpers for the six twin nodes.

Every twin follows the same shape:
1. Load its prompt from prompts/<twin>.md
2. Render the parsed context + (optional) Stage 2/3 inputs into the user message
3. Call Claude with a Pydantic response model that mirrors PhaseEstimate
4. Apply role attribution + return {"pass1_estimates": [PhaseEstimate(...)]} or
   {"pass2_estimates": [...]} based on the `pass` arg
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from models.estimation_state import EstimationState
from models.project_schema import AiToolingLevel, RoleRoster, Stage3Context
from models.twin_outputs import (
    Assumption,
    HourRange,
    Phase,
    PhaseEstimate,
    Risk,
)
from observability.langfuse_wrapper import traced
from orchestrator.ai_acceleration import band_for, effective_ai_reduction
from orchestrator.llm import call_structured, render_context_block
from orchestrator.montecarlo import Range3, ReductionSampler, make_rng, sample_pert

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_PHASE_VALUES = {p.value for p in Phase}


def load_prompt(name: str) -> str:
    """Load a twin's system prompt from prompts/<name>.md."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _calibration_for_phase(state: EstimationState, phase_value: str) -> list[dict[str, Any]]:
    """Filter the global calibration_examples down to entries for one phase."""
    rows = state.get("calibration_examples") or []
    return [r for r in rows if r.get("phase") == phase_value]


def _reduction_guardrail(
    state: EstimationState, stage3: Stage3Context, phase_value: str
) -> dict[str, Any] | None:
    """The active AI-reduction guardrail band for this phase's tooling level.

    Surfaces the same ``[lo, hi]`` band that ``effective_ai_reduction`` will clamp the
    twin's proposal into, so the LLM proposes *within* the guardrail rather than being
    silently clamped after the fact. Returns None when there's no tooling for the phase
    (band hi == 0) — nothing to propose. Bands come from the DB-loaded overrides in
    state, falling back to ``DEFAULT_BANDS``.
    """
    if phase_value not in _PHASE_VALUES:
        return None
    phase = Phase(phase_value)
    tooling = getattr(stage3.ai_tooling, phase_value)
    lo, hi = band_for(phase, tooling, state.get("reduction_bands"))
    if hi <= 0.0:
        return None
    return {
        "tooling_level": tooling.value,
        "min_pct": round(lo * 100, 1),
        "max_pct": round(hi * 100, 1),
        "note": (
            "Propose your reduction WITHIN this min–max band (percent). Values outside "
            "it are clamped to the band. The system then scales the result down by "
            "codebase context and team seniority and may net negative."
        ),
    }


def build_twin_user_prompt(
    state: EstimationState, pass_num: int, *, phase_value: str | None = None
) -> str:
    cal_rows = len(_calibration_for_phase(state, phase_value)) if phase_value else 0
    logger.debug(
        "build_twin_user_prompt phase=%s pass=%s calibration_rows=%d",
        phase_value,
        pass_num,
        cal_rows,
    )
    parsed = state.get("parsed_context", {})
    stage2 = state.get("stage2")
    stage3 = state.get("stage3") or Stage3Context()

    extras: dict[str, Any] = {
        "stage2": stage2.model_dump() if stage2 else None,
        "stage3": stage3.model_dump(),
        "pass": pass_num,
    }
    if pass_num == 2:
        questions = state.get("clarifying_questions", [])
        extras["user_answers"] = [
            {"question": q.text, "answer": q.answer or q.suggested_default}
            for q in questions
        ]

    # Historical calibration for the calling phase, if known. Helps the twin
    # anchor its UCP/FP/SLOC → hours mapping against prior projects with
    # matching industry / project_type / maturity. Absent on cold start.
    if phase_value:
        cal = _calibration_for_phase(state, phase_value)
        if cal:
            extras["calibration"] = cal
        guardrail = _reduction_guardrail(state, stage3, phase_value)
        if guardrail:
            extras["ai_reduction_guardrail"] = guardrail

    return (
        f"## Pass {pass_num}\n\n"
        f"Project description (raw):\n```\n{state.get('raw_input', '')}\n```\n\n"
        f"Structured context:\n{render_context_block(parsed, extras)}\n\n"
        f"Produce your phase estimate using the algorithm in your system prompt."
    )


def stub_phase_estimate(
    phase: Phase,
    twin_name: str,
    algorithm: str,
    ai_mid: float,
    manual_mid: float,
    roster: RoleRoster,
) -> PhaseEstimate:
    """Build a deterministic placeholder estimate.

    Used by the stub-twin path so the graph runs end-to-end without an LLM call,
    and as a fallback if a real twin's LLM call fails.
    """
    from orchestrator.role_attribution import attribute_roles

    return PhaseEstimate(
        phase=phase,
        twin_name=twin_name,
        algorithm=algorithm,
        ai_assisted_hours=HourRange(
            optimistic=ai_mid * 0.8, most_likely=ai_mid, pessimistic=ai_mid * 1.3
        ),
        manual_only_hours=HourRange(
            optimistic=manual_mid * 0.8, most_likely=manual_mid, pessimistic=manual_mid * 1.3
        ),
        ai_assisted_role_hours=attribute_roles(ai_mid, roster, phase),
        manual_only_role_hours=attribute_roles(manual_mid, roster, phase),
        assumptions=[Assumption(text="Stub estimate — twin not yet implemented", impact_hours=0)],
        risks=[Risk(description="Placeholder", likelihood=0.0, impact_hours_low=0, impact_hours_high=0)],
        gaps=[],
        confidence=0.3,
        notes="Stub output. Replace with real twin implementation.",
    )


def risk_specs_from(risks: list[Any]) -> list[tuple[float, float, float]]:
    """Map a twin's ``RiskInput`` list onto the ``(probability, low, high)`` tuples
    ``montecarlo.propagate_phase`` fires as independent Bernoulli risk events."""
    return [(rk.probability, rk.impact_hours_low, rk.impact_hours_high) for rk in risks]


def risks_from_inputs(risks: list[Any]) -> list[Risk]:
    """Map a twin's ``RiskInput`` list 1:1 onto output ``Risk`` objects
    (``probability → likelihood``)."""
    return [
        Risk(
            description=rk.description,
            likelihood=rk.probability,
            impact_hours_low=rk.impact_hours_low,
            impact_hours_high=rk.impact_hours_high,
        )
        for rk in risks
    ]


def assemble_phase_estimate(
    *,
    phase: Phase,
    twin_name: str,
    algorithm: str,
    point_mid: float,
    ai_mid: float,
    manual_mc: Any,
    ai_mc: Any,
    roster: RoleRoster,
    inputs: Any,
    breakdown: dict,
    effective_reduction: float,
    assumption_impact_factor: float,
    notes: str,
) -> PhaseEstimate:
    """Assemble the final ``PhaseEstimate`` shared by every twin's ``build_fn``.

    Each twin differs ONLY in ``phase`` / ``twin_name`` / ``algorithm`` / the
    ``assumption_impact_factor`` (per-twin, e.g. development 0.05 vs deployment 0.1)
    and the ``notes`` string it passes in. Everything else — the two
    ``result_to_hour_range`` mappings, the two ``attribute_roles`` splits (manual off
    ``point_mid``, ai off ``ai_mid``), the assumption/risk mappings, the breakdown and
    ``effective_ai_reduction_pct`` — is byte-identical, so it lives here. The load-bearing
    invariants (``ai.most_likely == manual.most_likely × (1 − reduction)``, role hours sum to
    ``most_likely``, MC Optional fields untouched) hold because the inputs are computed by the
    caller exactly as before."""
    from orchestrator.montecarlo import result_to_hour_range
    from orchestrator.role_attribution import attribute_roles

    return PhaseEstimate(
        phase=phase,
        twin_name=twin_name,
        algorithm=algorithm,
        ai_assisted_hours=result_to_hour_range(ai_mc),
        manual_only_hours=result_to_hour_range(manual_mc),
        ai_assisted_role_hours=attribute_roles(ai_mid, roster, phase),
        manual_only_role_hours=attribute_roles(point_mid, roster, phase),
        assumptions=[
            Assumption(text=a, impact_hours=point_mid * assumption_impact_factor)
            for a in inputs.assumptions
        ],
        risks=risks_from_inputs(inputs.risks),
        gaps=inputs.gaps,
        confidence=inputs.confidence,
        breakdown=breakdown,
        effective_ai_reduction_pct=round(effective_reduction * 100, 1),
        notes=notes,
    )


def roster_for(state: EstimationState) -> RoleRoster:
    """Pull the roster from Stage 2; fall back to the default if absent."""
    stage2 = state.get("stage2")
    return stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()


def tooling_for(stage3: Stage3Context, phase: Phase) -> AiToolingLevel:
    """The Stage-3 AI tooling level for a phase. Phase values map 1:1 onto the
    PhaseToolingLevels field names, so resolve by the phase's string value."""
    return getattr(stage3.ai_tooling, phase.value)


def make_reduction_sampler(
    *,
    reduction_ctx: dict[str, Any],
    proposed_point: float | None,
    reduction_range: Range3 | None,
) -> ReductionSampler:
    """Build the per-draw realized-AI-reduction sampler the twins feed to
    ``montecarlo.propagate_phase``.

    It samples the *proposed* reduction — from the twin's ``reduction_range`` (a %
    band), or a default spread around the proposed point, or (for Discovery/UX, which
    don't propose one) the guardrail band itself — then re-runs the deterministic
    ``effective_ai_reduction`` so the clamp / codebase·seniority moderation / penalty
    nonlinearity is honored on every draw. Returns a constant 0 when the phase has no
    AI-tooling band (no reduction, no spread)."""
    phase = reduction_ctx["phase"]
    tooling = reduction_ctx["tooling"]
    lo, hi = band_for(phase, tooling, reduction_ctx.get("bands"))
    if hi <= 0.0:
        return lambda rng: 0.0
    if proposed_point is not None:
        mode = proposed_point
        if reduction_range is not None:
            p_lo, p_hi = reduction_range.low / 100.0, reduction_range.high / 100.0
        else:
            p_lo, p_hi = mode * 0.55, mode * 1.6  # ~CoV 0.3 default spread
    else:
        mode = (lo + hi) / 2.0  # Discovery/UX: spread the proposal over the band
        p_lo, p_hi = lo, hi
    p_lo = max(0.0, min(p_lo, mode))
    p_hi = max(p_hi, mode)

    def _sampler(rng: random.Random) -> float:
        proposed = sample_pert(p_lo, mode, p_hi, rng)
        return effective_ai_reduction(proposed_reduction=proposed, **reduction_ctx)

    return _sampler


# A twin's algorithm-specific math: turn validated LLM inputs + the effective
# reduction + roster into the phase estimate. Each twin supplies its own.
BuildFn = Callable[..., PhaseEstimate]
# Optional hook to read a twin-proposed reduction (0..1) off its inputs model.
# Twins that don't let the LLM propose a reduction (discovery, ux) pass None.
# Typed over the concrete inputs model at each call site, so the parameter is
# Any here to stay assignable from any twin's narrower signature.
ProposedReductionFn = Callable[[Any], float]
# Optional self-consistency collapse: fold K independently-sampled inputs models into one
# consensus model (e.g. median of the numeric drivers). Twins that don't ensemble pass None.
EnsembleAggregateFn = Callable[[list[Any]], Any]


async def run_twin[T: BaseModel](
    state: EstimationState,
    pass_num: int,
    *,
    phase: Phase,
    prompt_name: str,
    tool_name: str,
    response_model: type[T],
    build_fn: BuildFn,
    stub_algorithm: str,
    stub_ai_mid: float,
    stub_manual_mid: float,
    proposed_reduction_fn: ProposedReductionFn | None = None,
    ensemble_k: int = 1,
    ensemble_aggregate_fn: EnsembleAggregateFn | None = None,
) -> PhaseEstimate:
    """Shared twin execution: prologue → LLM call → effective reduction →
    twin-specific ``build_fn`` → log, with a deterministic stub fallback.

    Only ``build_fn`` (and the proposed-reduction hook) differs between twins;
    everything else is identical plumbing hoisted out of the six twin modules.

    When ``ensemble_k > 1`` and an ``ensemble_aggregate_fn`` is supplied, **Pass 2** fires
    ``ensemble_k`` identical calls concurrently and folds them into one consensus inputs model
    (self-consistency) — used to damp the run-to-run noise of a high-variance twin. The frontier
    models ignore ``temperature`` (see ``llm._model_accepts_sampling_params``), so averaging
    samples is the only lever; the shared ``AsyncAnthropic`` client pools + auto-retries 429s.
    """
    stage2 = state.get("stage2")
    stage3 = state.get("stage3") or Stage3Context()
    roster = roster_for(state)
    regulated = bool(stage2 and getattr(stage2, "regulatory_requirements", None))
    twin_name = prompt_name

    try:
        system = load_prompt(prompt_name)
        user = build_twin_user_prompt(state, pass_num=pass_num, phase_value=phase.value)

        async def _call() -> T:
            return await call_structured(
                system=system, user=user, response_model=response_model, tool_name=tool_name
            )

        if pass_num == 2 and ensemble_k > 1 and ensemble_aggregate_fn is not None:
            results = await asyncio.gather(
                *(_call() for _ in range(ensemble_k)), return_exceptions=True
            )
            samples: list[T] = [r for r in results if not isinstance(r, BaseException)]
            if not samples:
                raise next(r for r in results if isinstance(r, BaseException))
            inputs = ensemble_aggregate_fn(samples) if len(samples) > 1 else samples[0]
            logger.info(
                "%s twin pass=2 self-consistency: aggregated %d/%d samples",
                phase.value,
                len(samples),
                ensemble_k,
            )
        else:
            inputs = await _call()
        proposed_point = proposed_reduction_fn(inputs) if proposed_reduction_fn is not None else None
        reduction_ctx: dict[str, Any] = {
            "phase": phase,
            "codebase": stage3.codebase_context,
            "tooling": tooling_for(stage3, phase),
            "roster": roster,
            "regulated": regulated,
            "bands": state.get("reduction_bands"),
        }
        eff = effective_ai_reduction(proposed_reduction=proposed_point, **reduction_ctx)
        rng = make_rng(f"{state.get('estimate_id', '')}:{phase.value}:{pass_num}")
        reduction_sampler = make_reduction_sampler(
            reduction_ctx=reduction_ctx,
            proposed_point=proposed_point,
            reduction_range=getattr(inputs, "reduction_range", None),
        )
        est = build_fn(
            inputs,
            effective_reduction=eff,
            roster=roster,
            rng=rng,
            reduction_sampler=reduction_sampler,
        )
        logger.info(
            "%s twin done: pass=%s ai_ml=%.0fh manual_ml=%.0fh",
            phase.value,
            pass_num,
            est.ai_assisted_hours.most_likely,
            est.manual_only_hours.most_likely,
        )
        return est
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s twin failed (%s); returning stub estimate", phase.value, exc)
        return stub_phase_estimate(
            phase, twin_name, stub_algorithm, stub_ai_mid, stub_manual_mid, roster
        )


def make_twin_nodes[T: BaseModel](
    *,
    phase: Phase,
    prompt_name: str,
    tool_name: str,
    response_model: type[T],
    build_fn: BuildFn,
    stub_algorithm: str,
    stub_ai_mid: float,
    stub_manual_mid: float,
    proposed_reduction_fn: ProposedReductionFn | None = None,
    ensemble_k: int = 1,
    ensemble_aggregate_fn: EnsembleAggregateFn | None = None,
    trace_name: str,
) -> tuple[
    Callable[[EstimationState], Awaitable[dict]],
    Callable[[EstimationState], Awaitable[dict]],
]:
    """Build a twin's two LangGraph node functions (pass 1 / pass 2).

    Returns ``(pass1_node, pass2_node)``. Each is ``@traced`` and returns
    ``{"pass1_estimates": [...]}`` / ``{"pass2_estimates": [...]}`` respectively —
    the only structural difference between the two passes. ``ensemble_k`` /
    ``ensemble_aggregate_fn`` opt a twin into Pass-2 self-consistency (default off).
    """

    async def _run(state: EstimationState, pass_num: int) -> PhaseEstimate:
        return await run_twin(
            state,
            pass_num,
            phase=phase,
            prompt_name=prompt_name,
            tool_name=tool_name,
            response_model=response_model,
            build_fn=build_fn,
            stub_algorithm=stub_algorithm,
            stub_ai_mid=stub_ai_mid,
            stub_manual_mid=stub_manual_mid,
            proposed_reduction_fn=proposed_reduction_fn,
            ensemble_k=ensemble_k,
            ensemble_aggregate_fn=ensemble_aggregate_fn,
        )

    @traced(name=f"{trace_name}.p1")
    async def pass1(state: EstimationState) -> dict:
        return {"pass1_estimates": [await _run(state, pass_num=1)]}

    @traced(name=f"{trace_name}.p2")
    async def pass2(state: EstimationState) -> dict:
        return {"pass2_estimates": [await _run(state, pass_num=2)]}

    return pass1, pass2

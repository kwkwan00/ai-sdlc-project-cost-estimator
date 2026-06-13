"""Shared helpers for the six twin nodes.

Every twin follows the same shape:
1. Load its prompt from prompts/<twin>.md
2. Render the parsed context + (optional) Stage 2/3 inputs into the user message
3. Call Claude with a Pydantic response model that mirrors PhaseEstimate
4. Apply role attribution + return {"pass1_estimates": [PhaseEstimate(...)]} or
   {"pass2_estimates": [...]} based on the `pass` arg
"""

from __future__ import annotations

import logging
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


def roster_for(state: EstimationState) -> RoleRoster:
    """Pull the roster from Stage 2; fall back to the default if absent."""
    stage2 = state.get("stage2")
    return stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()


def tooling_for(stage3: Stage3Context, phase: Phase) -> AiToolingLevel:
    """The Stage-3 AI tooling level for a phase. Phase values map 1:1 onto the
    PhaseToolingLevels field names, so resolve by the phase's string value."""
    return getattr(stage3.ai_tooling, phase.value)


# A twin's algorithm-specific math: turn validated LLM inputs + the effective
# reduction + roster into the phase estimate. Each twin supplies its own.
BuildFn = Callable[..., PhaseEstimate]
# Optional hook to read a twin-proposed reduction (0..1) off its inputs model.
# Twins that don't let the LLM propose a reduction (discovery, ux) pass None.
# Typed over the concrete inputs model at each call site, so the parameter is
# Any here to stay assignable from any twin's narrower signature.
ProposedReductionFn = Callable[[Any], float]


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
) -> PhaseEstimate:
    """Shared twin execution: prologue → LLM call → effective reduction →
    twin-specific ``build_fn`` → log, with a deterministic stub fallback.

    Only ``build_fn`` (and the proposed-reduction hook) differs between twins;
    everything else is identical plumbing hoisted out of the six twin modules.
    """
    stage2 = state.get("stage2")
    stage3 = state.get("stage3") or Stage3Context()
    roster = roster_for(state)
    regulated = bool(stage2 and getattr(stage2, "regulatory_requirements", None))
    twin_name = prompt_name

    try:
        inputs = await call_structured(
            system=load_prompt(prompt_name),
            user=build_twin_user_prompt(state, pass_num=pass_num, phase_value=phase.value),
            response_model=response_model,
            tool_name=tool_name,
        )
        reduction_kwargs: dict[str, Any] = {}
        if proposed_reduction_fn is not None:
            reduction_kwargs["proposed_reduction"] = proposed_reduction_fn(inputs)
        eff = effective_ai_reduction(
            phase=phase,
            codebase=stage3.codebase_context,
            tooling=tooling_for(stage3, phase),
            roster=roster,
            regulated=regulated,
            bands=state.get("reduction_bands"),
            **reduction_kwargs,
        )
        est = build_fn(inputs, effective_reduction=eff, roster=roster)
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
    trace_name: str,
) -> tuple[
    Callable[[EstimationState], Awaitable[dict]],
    Callable[[EstimationState], Awaitable[dict]],
]:
    """Build a twin's two LangGraph node functions (pass 1 / pass 2).

    Returns ``(pass1_node, pass2_node)``. Each is ``@traced`` and returns
    ``{"pass1_estimates": [...]}`` / ``{"pass2_estimates": [...]}`` respectively —
    the only structural difference between the two passes.
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
        )

    @traced(name=f"{trace_name}.p1")
    async def pass1(state: EstimationState) -> dict:
        return {"pass1_estimates": [await _run(state, pass_num=1)]}

    @traced(name=f"{trace_name}.p2")
    async def pass2(state: EstimationState) -> dict:
        return {"pass2_estimates": [await _run(state, pass_num=2)]}

    return pass1, pass2

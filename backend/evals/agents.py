"""Agent adapters — invoke each LLM agent in isolation and produce an AgentSample.

One adapter per agent (10 total). Each constructs the agent's inputs from
``case.input``, runs the agent, and fills the human-readable renderings + the
discrete ``retrieval_context`` items the judges score. Agent invocation is wrapped
in try/except so a failure becomes an ``AgentSample`` with ``error`` set rather
than aborting the batch.

The twins are driven through their pass-1 node functions (the same callables the
graph wires up), so we exercise the real ``run_twin`` plumbing including the
stub fallback. ``retrieval_context`` is captured by re-rendering
``build_twin_user_prompt`` and splitting it into one item per top-level context key.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from models.estimation_state import EstimationState
from models.project_schema import RoleRoster, Stage2Context, Stage3Context
from models.twin_outputs import Gap, Phase, PhaseEstimate
from orchestrator.nodes._twin_base import build_twin_user_prompt, roster_for, tooling_for

from .models import AgentSample, EvalCase

logger = logging.getLogger(__name__)


class AgentAdapter(Protocol):
    """Runs one EvalCase through one agent and returns the resulting sample."""

    async def run(self, case: EvalCase) -> AgentSample: ...


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _stage2_from_input(data: dict[str, Any]) -> Stage2Context | None:
    raw = data.get("stage2")
    if raw is None:
        return None
    if isinstance(raw, Stage2Context):
        return raw
    return Stage2Context.model_validate(raw)


def _stage3_from_input(data: dict[str, Any]) -> Stage3Context:
    raw = data.get("stage3")
    if raw is None:
        return Stage3Context()
    if isinstance(raw, Stage3Context):
        return raw
    return Stage3Context.model_validate(raw)


def _twin_retrieval_context(state: EstimationState, phase_value: str) -> list[str]:
    """Discrete context items the twin saw, one per top-level context key.

    We mirror what ``build_twin_user_prompt`` assembles: the raw input, each
    parsed_context field, the stage2 + stage3 summaries, each calibration entry,
    and the reduction guardrail (when present). This is what the precision/recall
    judges score, so it must reflect the real assembled context.
    """
    items: list[str] = []
    raw = state.get("raw_input") or ""
    if raw:
        items.append(f"raw_input: {raw}")

    parsed = state.get("parsed_context") or {}
    for key, value in parsed.items():
        items.append(f"parsed_context.{key}: {json.dumps(value, default=str)}")

    stage2 = state.get("stage2")
    if stage2 is not None:
        items.append(f"stage2: {json.dumps(stage2.model_dump(), default=str)}")

    stage3 = state.get("stage3")
    if stage3 is not None:
        items.append(f"stage3: {json.dumps(stage3.model_dump(), default=str)}")

    for row in state.get("calibration_examples") or []:
        if row.get("phase") == phase_value:
            items.append(f"calibration: {json.dumps(row, default=str)}")

    return items


def _build_twin_adapter(phase: Phase, node: Any) -> AgentAdapter:
    """Construct an adapter that drives a twin via its pass-1 node function."""

    class _TwinAdapter:
        async def run(self, case: EvalCase) -> AgentSample:
            data = case.input
            state: EstimationState = {
                "raw_input": data.get("raw_input", ""),
                "parsed_context": data.get("parsed_context", {}),
                "stage2": _stage2_from_input(data),
                "stage3": _stage3_from_input(data),
                "calibration_examples": data.get("calibration_examples", []),
                "reduction_bands": data.get("reduction_bands", {}),
            }
            task_input = build_twin_user_prompt(state, 1, phase_value=phase.value)
            retrieval = _twin_retrieval_context(state, phase.value)
            # Structured bits the deterministic twin rubrics recompute against.
            stage3 = state["stage3"]
            roster = roster_for(state)
            eval_context: dict[str, Any] = {
                "phase": phase.value,
                "tooling_level": tooling_for(stage3, phase).value,
                "reduction_bands": state.get("reduction_bands", {}),
                "roster": roster.model_dump(),
            }
            try:
                result = await node(state)
                estimate: PhaseEstimate = result["pass1_estimates"][0]
            except Exception as exc:  # noqa: BLE001
                logger.warning("twin %s adapter failed: %s", phase.value, exc)
                return AgentSample(
                    case_id=case.id,
                    agent=case.agent,
                    task_input=task_input,
                    retrieval_context=retrieval,
                    expected_output=case.expected_output,
                    gold=case.gold,
                    eval_context=eval_context,
                    error=str(exc),
                )
            is_stub = _is_stub_estimate(estimate)
            return AgentSample(
                case_id=case.id,
                agent=case.agent,
                task_input=task_input,
                output_text=_render_estimate(estimate),
                output_obj=estimate,
                retrieval_context=retrieval,
                expected_output=case.expected_output,
                gold=case.gold,
                eval_context=eval_context,
                is_stub=is_stub,
            )

    return _TwinAdapter()


# Marker text emitted by ``stub_phase_estimate`` in _twin_base.py. When the twin's
# LLM call fails, run_twin returns this deterministic placeholder. We detect it via
# the notes string + the fixed 0.3 confidence it sets, so json_correctness can fail
# a stub even though it is structurally valid.
_STUB_NOTES_MARKER = "Stub output. Replace with real twin implementation."


def _is_stub_estimate(estimate: PhaseEstimate) -> bool:
    return estimate.notes.strip() == _STUB_NOTES_MARKER and estimate.confidence == 0.3


def _render_estimate(est: PhaseEstimate) -> str:
    """Human-readable rendering of a PhaseEstimate for the plan_quality judge."""
    lines = [
        f"phase={est.phase.value} algorithm={est.algorithm} confidence={est.confidence}",
        f"ai_assisted_hours (o/m/p): {est.ai_assisted_hours.optimistic}/"
        f"{est.ai_assisted_hours.most_likely}/{est.ai_assisted_hours.pessimistic}",
        f"manual_only_hours (o/m/p): {est.manual_only_hours.optimistic}/"
        f"{est.manual_only_hours.most_likely}/{est.manual_only_hours.pessimistic}",
        f"effective_ai_reduction_pct: {est.effective_ai_reduction_pct}",
    ]
    if est.breakdown:
        lines.append(f"breakdown: {json.dumps(est.breakdown, default=str)}")
    if est.assumptions:
        lines.append("assumptions: " + " | ".join(a.text for a in est.assumptions))
    if est.risks:
        lines.append("risks: " + " | ".join(r.description for r in est.risks))
    if est.gaps:
        lines.append("gaps: " + " | ".join(g.question_text for g in est.gaps))
    if est.notes:
        lines.append(f"notes: {est.notes}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Non-twin adapters
# --------------------------------------------------------------------------- #


class _PrefillAdapter:
    async def run(self, case: EvalCase) -> AgentSample:
        from prefill import run_prefill_agent

        raw_input = case.input.get("raw_input", "")
        task_input = f"Extract + normalize Stage 2 context from:\n{raw_input}"
        try:
            result = await run_prefill_agent(raw_input)
        except Exception as exc:  # noqa: BLE001
            return AgentSample(
                case_id=case.id,
                agent=case.agent,
                task_input=task_input,
                retrieval_context=[f"raw_input: {raw_input}"],
                source_text=raw_input,
                expected_output=case.expected_output,
                gold=case.gold,
                error=str(exc),
            )
        return AgentSample(
            case_id=case.id,
            agent=case.agent,
            task_input=task_input,
            # summarization scores output_text (the summary) against source_text;
            # extraction_accuracy scores output_obj (NormalizedProjectContext) vs gold.
            output_text=result.summary,
            output_obj=result,
            retrieval_context=[f"raw_input: {raw_input}"],
            source_text=raw_input,
            expected_output=case.expected_output,
            gold=case.gold,
        )


class _RosterAdapter:
    async def run(self, case: EvalCase) -> AgentSample:
        from roster_agent import CatalogRole, run_roster_agent

        raw_input = case.input.get("raw_input", "")
        # Optional org rate-card catalog the agent may SELECT from (the roster_catalog_selection
        # rubric checks whether it picked gold["expected_catalog_role_id"]).
        catalog = [
            CatalogRole(c["role_id"], c["label"], c["category"], c["seniority"], float(c["rate"]))
            for c in case.input.get("custom_roles", [])
        ]
        stage2 = _stage2_from_input(case.input) or Stage2Context()
        retrieval = [f"raw_input: {raw_input}"]
        retrieval.extend(
            f"stage2.{key}: {json.dumps(value, default=str)}"
            for key, value in {
                "industry": stage2.industry,
                "project_type": stage2.project_type.value,
                "screen_count_estimate": stage2.screen_count_estimate,
                "integration_count": stage2.integration_count,
                "integration_list": stage2.integration_list,
                "regulatory_requirements": stage2.regulatory_requirements,
            }.items()
        )
        task_input = "Propose a delivery plan + team roster for the project."
        # staffing_adequacy derives required categories from these Stage 2 signals.
        eval_context: dict[str, Any] = {
            "stage2_signals": {
                "screen_count": stage2.screen_count_estimate,
                "regulatory": list(stage2.regulatory_requirements),
            }
        }
        try:
            proposal = await run_roster_agent(stage2, raw_input, custom_roles=catalog or None)
        except Exception as exc:  # noqa: BLE001
            return AgentSample(
                case_id=case.id,
                agent=case.agent,
                task_input=task_input,
                retrieval_context=retrieval,
                expected_output=case.expected_output,
                gold=case.gold,
                eval_context=eval_context,
                error=str(exc),
            )
        plan = "; ".join(f"{p.workstream}: {p.summary}" for p in proposal.project_plan)
        roles = "; ".join(
            f"{r.description} [{r.category.value}/{r.seniority.value}] {r.percentage}%"
            for r in proposal.roles
        )
        output_text = (
            f"plan: {plan}\nrationale: {proposal.staffing_rationale}\nroles: {roles}"
        )
        return AgentSample(
            case_id=case.id,
            agent=case.agent,
            task_input=task_input,
            output_text=output_text,
            output_obj=proposal,
            retrieval_context=retrieval,
            expected_output=case.expected_output,
            gold=case.gold,
            eval_context=eval_context,
        )


class _ToolingAdapter:
    async def run(self, case: EvalCase) -> AgentSample:
        from tooling_classifier import classify_ai_tooling

        description = case.input.get("description", "")
        task_input = f"Classify per-phase AI tooling levels from:\n{description}"
        try:
            result = await classify_ai_tooling(description)
        except Exception as exc:  # noqa: BLE001
            return AgentSample(
                case_id=case.id,
                agent=case.agent,
                task_input=task_input,
                retrieval_context=[f"description: {description}"],
                expected_output=case.expected_output,
                gold=case.gold,
                error=str(exc),
            )
        output_text = (
            f"ai_tooling: {json.dumps(result.ai_tooling.model_dump(), default=str)}\n"
            f"unknown_tools: {result.unknown_tools}\nnotes: {result.notes}"
        )
        return AgentSample(
            case_id=case.id,
            agent=case.agent,
            task_input=task_input,
            output_text=output_text,
            output_obj=result,
            retrieval_context=[f"description: {description}"],
            expected_output=case.expected_output,
            gold=case.gold,
        )


class _ConsolidatorAdapter:
    async def run(self, case: EvalCase) -> AgentSample:
        from orchestrator.nodes.merge_pass1 import _consolidate_with_partition

        raw_candidates = case.input.get("candidates", [])
        candidates: list[tuple[Gap, list[Phase]]] = []
        for entry in raw_candidates:
            gap = Gap.model_validate(entry["gap"])
            phases = [Phase(p) for p in entry.get("phases", [])]
            candidates.append((gap, phases))
        question_texts = [gap.question_text for gap, _ in candidates]
        retrieval = [f"candidate: {text}" for text in question_texts]
        task_input = "Cluster near-duplicate clarifying questions:\n" + "\n".join(
            f"{i}. {text}" for i, text in enumerate(question_texts)
        )
        # partition_correctness scores the predicted cluster→input-index mapping
        # EXACTLY vs gold when present; input_phases is the proxy-fallback coverage.
        eval_context: dict[str, Any] = {
            "input_phases": [[p.value for p in phases] for _gap, phases in candidates]
        }
        try:
            merged, predicted_partition = await _consolidate_with_partition(candidates)
        except Exception as exc:  # noqa: BLE001
            return AgentSample(
                case_id=case.id,
                agent=case.agent,
                task_input=task_input,
                retrieval_context=retrieval,
                expected_output=case.expected_output,
                gold=case.gold,
                eval_context=eval_context,
                error=str(exc),
            )
        eval_context["predicted_partition"] = predicted_partition
        output_text = "merged questions:\n" + "\n".join(
            f"- {gap.question_text}" for gap, _ in merged
        )
        return AgentSample(
            case_id=case.id,
            agent=case.agent,
            task_input=task_input,
            output_text=output_text,
            output_obj=merged,
            retrieval_context=retrieval,
            expected_output=case.expected_output,
            gold=case.gold,
            eval_context=eval_context,
        )


class _WbsAdapter:
    async def run(self, case: EvalCase) -> AgentSample:
        from models.wbs_schema import WbsDraftRequest
        from wbs_agent import generate_wbs_tree

        raw_input = case.input.get("raw_input", "")
        stage2 = _stage2_from_input(case.input)
        req = WbsDraftRequest(raw_input=raw_input, stage2=stage2)
        roster = stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()
        retrieval = [f"raw_input: {raw_input}"]
        task_input = "Decompose the project into a Work Breakdown Structure (work packages → tasks)."
        # wbs_structural checks every leaf's role_id against the roster.
        eval_context: dict[str, Any] = {"roster_role_ids": [r.role_id for r in roster.roles]}
        try:
            # generate_wbs_tree ALWAYS returns a valid tree (deterministic fallback with no API key),
            # so wbs_structural is a real offline gate; the planner output is scored when a key is set.
            tree, notes = await generate_wbs_tree(req)
        except Exception as exc:  # noqa: BLE001
            return AgentSample(
                case_id=case.id, agent=case.agent, task_input=task_input,
                retrieval_context=retrieval, expected_output=case.expected_output,
                gold=case.gold, eval_context=eval_context, error=str(exc),
            )
        from models.wbs_task import count_tasks, iter_leaves

        leaves = list(iter_leaves(tree))
        output_text = (
            f"{count_tasks(tree)} tasks ({len(leaves)} leaves); notes: {notes}\n"
            + "\n".join(
                f"- [{leaf.phase.value if leaf.phase else '?'}] {leaf.name} "
                f"({leaf.role_id}) {leaf.optimistic}/{leaf.most_likely}/{leaf.pessimistic}h"
                for leaf in leaves
            )
        )
        return AgentSample(
            case_id=case.id, agent=case.agent, task_input=task_input,
            output_text=output_text, output_obj=tree, retrieval_context=retrieval,
            expected_output=case.expected_output, gold=case.gold, eval_context=eval_context,
        )


def _build_adapters() -> dict[str, AgentAdapter]:
    from orchestrator.nodes.code_review_sentinel import code_review_pass1
    from orchestrator.nodes.deployment_devops import deployment_pass1
    from orchestrator.nodes.development_architect import development_pass1
    from orchestrator.nodes.discovery_analyst import discovery_analyst_pass1
    from orchestrator.nodes.qa_testing_strategist import qa_testing_pass1
    from orchestrator.nodes.ux_design_strategist import ux_design_pass1

    adapters: dict[str, AgentAdapter] = {
        "discovery": _build_twin_adapter(Phase.DISCOVERY, discovery_analyst_pass1),
        "ux_design": _build_twin_adapter(Phase.UX_DESIGN, ux_design_pass1),
        "development": _build_twin_adapter(Phase.DEVELOPMENT, development_pass1),
        "code_review": _build_twin_adapter(Phase.CODE_REVIEW, code_review_pass1),
        "deployment": _build_twin_adapter(Phase.DEPLOYMENT, deployment_pass1),
        "qa_testing": _build_twin_adapter(Phase.QA_TESTING, qa_testing_pass1),
        "prefill": _PrefillAdapter(),
        "roster": _RosterAdapter(),
        "tooling": _ToolingAdapter(),
        "consolidator": _ConsolidatorAdapter(),
        "wbs": _WbsAdapter(),
    }
    return adapters


ADAPTERS: dict[str, AgentAdapter] = _build_adapters()

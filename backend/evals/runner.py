"""Eval runner — for each selected agent, run its cases through the adapter and
score each applicable rubric, with bounded concurrency.

Resilient by design: a per-case adapter failure or a per-rubric judge failure is
recorded on the result, never aborting the batch. Rubrics actually scored per
agent are ``AGENT_RUBRICS[agent]`` intersected with the requested ``rubrics``.

IMPORTANT: this path intentionally does NOT bind a usage accumulator
(``orchestrator.usage.bind_usage_accumulator``) — eval/judge token spend is kept
out of the per-estimate cost accounting.
"""

from __future__ import annotations

import asyncio
import logging
from statistics import fmean

from . import rubrics
from .agents import ADAPTERS
from .datasets import load_cases
from .models import (
    AGENT_RUBRICS,
    AgentReport,
    AgentSample,
    CaseResult,
    EvalReport,
    RubricName,
    RubricScore,
)

logger = logging.getLogger(__name__)


def _resolve_agents(agents: list[str] | None) -> list[str]:
    if agents:
        unknown = [a for a in agents if a not in ADAPTERS]
        if unknown:
            raise ValueError(f"Unknown agent(s): {unknown}. Known: {sorted(ADAPTERS)}")
        return agents
    return list(ADAPTERS)


def _rubrics_for_agent(
    agent: str, requested: list[RubricName] | None
) -> list[RubricName]:
    applicable = AGENT_RUBRICS.get(agent, [])
    if requested is None:
        return list(applicable)
    requested_set = set(requested)
    return [r for r in applicable if r in requested_set]


async def _score_sample(
    sample: AgentSample,
    rubric_names: list[RubricName],
    *,
    judge_model: str,
    sem: asyncio.Semaphore,
) -> list[RubricScore]:
    async def _one(name: RubricName) -> RubricScore:
        async with sem:
            return await rubrics.score(name, sample, judge_model=judge_model)

    return await asyncio.gather(*(_one(name) for name in rubric_names))


async def _run_case(
    agent: str,
    case_id: str,
    sample: AgentSample,
    rubric_names: list[RubricName],
    *,
    judge_model: str,
    sem: asyncio.Semaphore,
) -> CaseResult:
    scores = await _score_sample(
        sample, rubric_names, judge_model=judge_model, sem=sem
    )
    return CaseResult(
        case_id=case_id,
        agent=agent,
        scores=scores,
        sample_error=sample.error,
        is_stub=sample.is_stub,
    )


def _aggregate(agent: str, results: list[CaseResult]) -> AgentReport:
    means: dict[str, list[float]] = {}
    passes: dict[str, list[float]] = {}
    for result in results:
        for score in result.scores:
            if score.skipped:
                continue  # not applicable — exclude from means + pass-rates
            means.setdefault(score.rubric, []).append(score.score)
            passes.setdefault(score.rubric, []).append(1.0 if score.passed else 0.0)
    return AgentReport(
        agent=agent,
        case_count=len(results),
        rubric_means={r: fmean(v) for r, v in means.items() if v},
        rubric_pass_rates={r: fmean(v) for r, v in passes.items() if v},
        results=results,
    )


async def run_evals(
    *,
    agents: list[str] | None,
    rubrics: list[RubricName] | None,
    judge_model: str,
    concurrency: int = 4,
) -> EvalReport:
    """Run the eval matrix and return an aggregated report.

    ``agents`` / ``rubrics`` default to "all applicable" when None. Concurrency
    bounds the number of in-flight adapter runs + judge calls via a semaphore.
    """
    selected = _resolve_agents(agents)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run_agent(agent: str) -> AgentReport:
        adapter = ADAPTERS[agent]
        cases = load_cases(agent)
        rubric_names = _rubrics_for_agent(agent, rubrics)
        if not cases:
            logger.warning("no cases found for agent=%s; skipping", agent)
            return AgentReport(agent=agent, case_count=0)

        async def _one_case(case_id: str) -> CaseResult:
            async with sem:
                sample = await _safe_run_adapter(adapter, agent, case_id, cases)
            return await _run_case(
                agent,
                case_id,
                sample,
                rubric_names,
                judge_model=judge_model,
                sem=sem,
            )

        results = await asyncio.gather(*(_one_case(c.id) for c in cases))
        report = _aggregate(agent, list(results))
        logger.info(
            "agent=%s scored: %d case(s), means=%s",
            agent,
            report.case_count,
            {k: round(v, 3) for k, v in report.rubric_means.items()},
        )
        return report

    agent_reports = await asyncio.gather(*(_run_agent(a) for a in selected))
    return EvalReport(judge_model=judge_model, agents=list(agent_reports))


async def _safe_run_adapter(adapter, agent, case_id, cases) -> AgentSample:  # type: ignore[no-untyped-def]
    """Run the adapter for one case id, capturing any failure into the sample."""
    case = next(c for c in cases if c.id == case_id)
    try:
        return await adapter.run(case)
    except Exception as exc:  # noqa: BLE001
        logger.warning("adapter for agent=%s case=%s raised: %s", agent, case_id, exc)
        return AgentSample(
            case_id=case_id,
            agent=agent,
            expected_output=case.expected_output,
            error=str(exc),
        )

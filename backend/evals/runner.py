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
    NEEDS_MULTI_SAMPLE,
    AgentReport,
    AgentSample,
    CaseResult,
    EvalCase,
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
    samples: list[AgentSample],
    rubric_names: list[RubricName],
    *,
    judge_model: str,
    sem: asyncio.Semaphore,
) -> list[RubricScore]:
    """Score every rubric for a case. ``samples`` holds the case's adapter re-runs
    (length == repeats); single-sample rubrics read ``samples[0]`` while the
    multi-sample rubrics (``NEEDS_MULTI_SAMPLE``) consume the whole list via
    ``rubrics.score_multi``."""
    primary = samples[0]

    async def _one(name: RubricName) -> RubricScore:
        async with sem:
            if name in NEEDS_MULTI_SAMPLE and len(samples) > 1:
                return await rubrics.score_multi(name, samples, judge_model=judge_model)
            return await rubrics.score(name, primary, judge_model=judge_model)

    return await asyncio.gather(*(_one(name) for name in rubric_names))


async def _run_case(
    agent: str,
    case_id: str,
    samples: list[AgentSample],
    rubric_names: list[RubricName],
    *,
    judge_model: str,
    sem: asyncio.Semaphore,
) -> CaseResult:
    scores = await _score_sample(
        samples, rubric_names, judge_model=judge_model, sem=sem
    )
    primary = samples[0]
    return CaseResult(
        case_id=case_id,
        agent=agent,
        scores=scores,
        sample_error=primary.error,
        is_stub=primary.is_stub,
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
    repeats: int = 1,
    synthetic_cases: dict[str, list[EvalCase]] | None = None,
) -> EvalReport:
    """Run the eval matrix and return an aggregated report.

    ``agents`` / ``rubrics`` default to "all applicable" when None. Concurrency
    bounds the number of in-flight adapter runs + judge calls via a semaphore.

    ``repeats`` (default 1) is the number of times each case's adapter is re-run so
    the multi-sample rubrics (``consistency``, ``faithfulness``) can assess run-to-run
    behavior; at the default 1 every case runs exactly once and behavior is identical
    to before. ``synthetic_cases`` is an optional ``{agent: [EvalCase, ...]}`` map
    (e.g. from ``evals.synthetic.generate_cases``) folded in alongside the on-disk
    golden cases for that agent.
    """
    selected = _resolve_agents(agents)
    sem = asyncio.Semaphore(max(1, concurrency))
    n_repeats = max(1, repeats)
    extra = synthetic_cases or {}

    async def _run_agent(agent: str) -> AgentReport:
        adapter = ADAPTERS[agent]
        cases = [*load_cases(agent), *extra.get(agent, [])]
        rubric_names = _rubrics_for_agent(agent, rubrics)
        if not cases:
            logger.warning("no cases found for agent=%s; skipping", agent)
            return AgentReport(agent=agent, case_count=0)

        # Only re-run the adapter when a multi-sample rubric is actually in play.
        wants_multi = any(r in NEEDS_MULTI_SAMPLE for r in rubric_names)
        case_repeats = n_repeats if wants_multi else 1

        async def _one_case(case_id: str) -> CaseResult:
            samples: list[AgentSample] = []
            for _ in range(case_repeats):
                async with sem:
                    samples.append(await _safe_run_adapter(adapter, agent, case_id, cases))
            return await _run_case(
                agent,
                case_id,
                samples,
                rubric_names,
                judge_model=judge_model,
                sem=sem,
            )

        results = await asyncio.gather(*(_one_case(c.id) for c in cases))
        report = _aggregate(agent, list(results))
        logger.info(
            "agent=%s scored: %d case(s) x%d run(s), means=%s",
            agent,
            report.case_count,
            case_repeats,
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

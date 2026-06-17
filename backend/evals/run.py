"""CLI for the eval harness. Mirrors orchestrator/smoke.py.

Usage:
    uv run python -m evals.run
    uv run python -m evals.run --agent discovery --agent development
    uv run python -m evals.run --rubric plan_quality --json out.json
    uv run python -m evals.run --judge-model gpt-5.5 --concurrency 6   # default judge is gpt-5.5
    uv run python -m evals.run --synthetic 20 --synthetic-seed 7   # fold in 20 synthetic projects
    uv run python -m evals.run --repeats 3                          # consistency + faithfulness averaging

Exits nonzero if any rubric's overall mean is below its threshold, so CI can gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import cast

from config import get_settings

from . import report as report_mod
from .models import EvalCase, RubricName
from .runner import run_evals
from .synthetic import generate_cases_by_agent

logger = logging.getLogger(__name__)

_VALID_RUBRICS: tuple[str, ...] = (
    "faithfulness",
    "plan_quality",
    "summarization",
    "json_correctness",
    "band_adherence",
    "algorithm_conformance",
    "role_attribution_validity",
    "estimate_accuracy",
    "interval_calibration",
    "extraction_accuracy",
    "staffing_adequacy",
    "classification_accuracy",
    "enum_constraint_adherence",
    "partition_correctness",
    "consistency",
)


async def main(
    *,
    agents: list[str] | None,
    rubrics: list[str] | None,
    judge_model: str,
    json_path: str | None,
    concurrency: int,
    repeats: int,
    synthetic: int,
    synthetic_seed: int,
) -> int:
    rubric_names = (
        [cast(RubricName, r) for r in rubrics] if rubrics else None
    )
    synthetic_cases: dict[str, list[EvalCase]] | None = None
    if synthetic > 0:
        synthetic_cases = generate_cases_by_agent(synthetic, synthetic_seed)
        logger.info(
            "folding in %d synthetic project(s) (seed=%d) -> %d twin case(s)",
            synthetic,
            synthetic_seed,
            sum(len(v) for v in synthetic_cases.values()),
        )
    logger.info(
        "running evals: agents=%s rubrics=%s judge_model=%s concurrency=%d repeats=%d",
        agents or "all",
        rubrics or "all-applicable",
        judge_model,
        concurrency,
        repeats,
    )
    report = await run_evals(
        agents=agents,
        rubrics=rubric_names,
        judge_model=judge_model,
        concurrency=concurrency,
        repeats=repeats,
        synthetic_cases=synthetic_cases,
    )

    print(report_mod.render_text(report))

    if json_path:
        Path(json_path).write_text(
            json.dumps(report_mod.to_dict(report), indent=2), encoding="utf-8"
        )
        print(f"\nWrote JSON report to {json_path}")

    failing = report.failing_rubrics()
    if failing:
        print(f"\nFAIL: {len(failing)} rubric mean(s) below threshold.")
        return 1
    print("\nPASS: all rubric means at or above threshold.")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LLM-evaluation harness for every agent.")
    parser.add_argument(
        "--agent",
        action="append",
        dest="agents",
        help="Agent to evaluate (repeatable). Default: all.",
    )
    parser.add_argument(
        "--rubric",
        action="append",
        dest="rubrics",
        choices=_VALID_RUBRICS,
        help="Rubric to run (repeatable). Default: all applicable per agent.",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help=(
            "Override the LLM-as-judge model (default: OPENAI_MODEL_EVAL = gpt-5.5). "
            "A claude-* model also works (falls back to the Anthropic judge path)."
        ),
    )
    parser.add_argument("--json", dest="json_path", default=None, help="Write JSON report here.")
    parser.add_argument("--concurrency", type=int, default=4, help="Max in-flight calls.")
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help=(
            "Re-run each case's adapter N times so the consistency rubric can measure "
            "run-to-run stability and faithfulness can average over runs (default 1 = "
            "single run, unchanged behavior)."
        ),
    )
    parser.add_argument(
        "--synthetic",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Fold in N deterministically-generated synthetic projects (6 twin cases "
            "each) carrying gold actuals, activating interval_calibration. Default 0."
        ),
    )
    parser.add_argument(
        "--synthetic-seed",
        type=int,
        default=0,
        help="Seed for --synthetic generation (default 0; deterministic).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    judge_model = args.judge_model or get_settings().openai_model_eval
    raise SystemExit(
        asyncio.run(
            main(
                agents=args.agents,
                rubrics=args.rubrics,
                judge_model=judge_model,
                json_path=args.json_path,
                concurrency=args.concurrency,
                repeats=args.repeats,
                synthetic=args.synthetic,
                synthetic_seed=args.synthetic_seed,
            )
        )
    )

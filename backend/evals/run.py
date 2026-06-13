"""CLI for the eval harness. Mirrors orchestrator/smoke.py.

Usage:
    uv run python -m evals.run
    uv run python -m evals.run --agent discovery --agent development
    uv run python -m evals.run --rubric plan_quality --json out.json
    uv run python -m evals.run --judge-model claude-sonnet-4-6 --concurrency 6

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
from .models import RubricName
from .runner import run_evals

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
    "extraction_accuracy",
    "staffing_adequacy",
    "classification_accuracy",
    "enum_constraint_adherence",
    "partition_correctness",
)


async def main(
    *,
    agents: list[str] | None,
    rubrics: list[str] | None,
    judge_model: str,
    json_path: str | None,
    concurrency: int,
) -> int:
    rubric_names = (
        [cast(RubricName, r) for r in rubrics] if rubrics else None
    )
    logger.info(
        "running evals: agents=%s rubrics=%s judge_model=%s concurrency=%d",
        agents or "all",
        rubrics or "all-applicable",
        judge_model,
        concurrency,
    )
    report = await run_evals(
        agents=agents,
        rubrics=rubric_names,
        judge_model=judge_model,
        concurrency=concurrency,
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
        help="Override the LLM-as-judge model (default: ANTHROPIC_MODEL_EVAL).",
    )
    parser.add_argument("--json", dest="json_path", default=None, help="Write JSON report here.")
    parser.add_argument("--concurrency", type=int, default=4, help="Max in-flight calls.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    judge_model = args.judge_model or get_settings().anthropic_model_eval
    raise SystemExit(
        asyncio.run(
            main(
                agents=args.agents,
                rubrics=args.rubrics,
                judge_model=judge_model,
                json_path=args.json_path,
                concurrency=args.concurrency,
            )
        )
    )

"""Render an EvalReport as a readable text table + a JSON-serializable dict."""

from __future__ import annotations

from typing import Any

from .models import AGENT_RUBRICS, RUBRIC_THRESHOLDS, EvalReport, RubricName

# Stable column order for the rubric table.
_RUBRIC_ORDER: list[RubricName] = [
    "json_correctness",
    "faithfulness",
    "band_adherence",
    "algorithm_conformance",
    "role_attribution_validity",
    "estimate_accuracy",
    "interval_calibration",
    "consistency",
    "extraction_accuracy",
    "staffing_adequacy",
    "roster_catalog_selection",
    "classification_accuracy",
    "enum_constraint_adherence",
    "partition_correctness",
    "wbs_structural",
    "plan_quality",
    "summarization",
]

_SHORT = {
    "json_correctness": "json_ok",
    "faithfulness": "faith",
    "band_adherence": "band",
    "algorithm_conformance": "algo",
    "role_attribution_validity": "roles",
    "estimate_accuracy": "est_acc",
    "interval_calibration": "interval",
    "consistency": "consist",
    "extraction_accuracy": "extract",
    "staffing_adequacy": "staff",
    "roster_catalog_selection": "cat_sel",
    "classification_accuracy": "classif",
    "enum_constraint_adherence": "enum",
    "partition_correctness": "partn",
    "wbs_structural": "wbs_str",
    "plan_quality": "plan_q",
    "summarization": "summ",
}


def _cell(report_means: dict[str, float], rubric: RubricName, applicable: bool) -> str:
    if not applicable:
        return "   -  "
    if rubric not in report_means:
        return "   .  "
    mean = report_means[rubric]
    flag = "P" if mean >= RUBRIC_THRESHOLDS[rubric] else "F"
    return f"{mean:.2f}{flag} "


def render_text(report: EvalReport) -> str:
    """A monospace agent x rubric table + overall pass-rate footer."""
    lines: list[str] = []
    lines.append(f"Eval report (judge_model={report.judge_model})")
    lines.append("=" * 72)

    header = f"{'agent':<14}" + "".join(f"{_SHORT[r]:>10}" for r in _RUBRIC_ORDER)
    header += f"{'cases':>7}"
    lines.append(header)
    lines.append("-" * len(header))

    for agent in report.agents:
        applicable = set(AGENT_RUBRICS.get(agent.agent, []))
        row = f"{agent.agent:<14}"
        for rubric in _RUBRIC_ORDER:
            row += f"{_cell(agent.rubric_means, rubric, rubric in applicable):>10}"
        row += f"{agent.case_count:>7}"
        lines.append(row)

    lines.append("-" * len(header))
    means = report.rubric_means()
    overall = f"{'OVERALL mean':<14}"
    for rubric in _RUBRIC_ORDER:
        if rubric in means:
            mean = means[rubric]
            flag = "P" if mean >= RUBRIC_THRESHOLDS[rubric] else "F"
            overall += f"{mean:.2f}{flag} ".rjust(10)
        else:
            overall += f"{'.':>10}"
    lines.append(overall)

    lines.append("")
    lines.append(f"Overall pass-rate: {report.overall_pass_rate:.1%}")
    failing = report.failing_rubrics()
    if failing:
        detail = ", ".join(
            f"{r}={m:.2f}<{RUBRIC_THRESHOLDS[r]}" for r, m in failing.items()  # type: ignore[index]
        )
        lines.append(f"FAILING rubric means: {detail}")
    else:
        lines.append("All rubric means at or above threshold.")

    # Surface sample/judge errors so they aren't silently buried in the means.
    errors: list[str] = []
    for agent in report.agents:
        for result in agent.results:
            if result.sample_error:
                errors.append(
                    f"  [{agent.agent}/{result.case_id}] adapter error: {result.sample_error}"
                )
            for score in result.scores:
                if score.error:
                    errors.append(
                        f"  [{agent.agent}/{result.case_id}/{score.rubric}] judge error: {score.error}"
                    )
    if errors:
        lines.append("")
        lines.append(f"Errors ({len(errors)}):")
        lines.extend(errors[:50])
        if len(errors) > 50:
            lines.append(f"  ... and {len(errors) - 50} more")

    return "\n".join(lines)


def to_dict(report: EvalReport) -> dict[str, Any]:
    """JSON-serializable view of the report for ``--json`` output."""
    payload = report.model_dump(mode="json")
    payload["overall_pass_rate"] = report.overall_pass_rate
    payload["rubric_means"] = report.rubric_means()
    payload["failing_rubrics"] = report.failing_rubrics()
    return payload

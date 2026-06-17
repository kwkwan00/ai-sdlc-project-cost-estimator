"""Shared data contract for the LLM-evaluation harness.

This module pins every interface the rest of the package builds on:
- ``RubricName`` — the five rubric identifiers.
- ``EvalCase`` — a golden case loaded from ``datasets/*.json``.
- ``AgentSample`` — what an agent adapter produces from a case (the agent's
  rendered task + output + the discrete context items it saw), which the judges
  then score.
- ``RubricScore`` / ``CaseResult`` / ``AgentReport`` / ``EvalReport`` — the
  aggregates the runner + reporter consume.
- ``RUBRIC_THRESHOLDS`` — per-rubric pass thresholds.
- ``AGENT_RUBRICS`` — the agent→rubric applicability matrix.

No LLM imports here on purpose: this is the contract every other module shares,
including the offline tests.
"""

from __future__ import annotations

from statistics import fmean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RubricName = Literal[
    # Judge (LLM) rubrics.
    "faithfulness",
    "plan_quality",
    "summarization",
    # Deterministic correctness rubrics.
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
    # Self-consistency rubric (deterministic scorer over N adapter re-runs).
    "consistency",
]

# The ten agents under evaluation: the six estimation twins plus the four
# pre-/post-estimate agents.
TWIN_AGENTS: tuple[str, ...] = (
    "discovery",
    "ux_design",
    "development",
    "code_review",
    "deployment",
    "qa_testing",
)
ALL_AGENTS: tuple[str, ...] = (
    *TWIN_AGENTS,
    "prefill",
    "roster",
    "tooling",
    "consolidator",
)

# Applicability matrix. The two RAG retriever metrics (context_precision /
# contextual_recall) were removed — these are non-retrieval agents. Each agent now
# gets the high-value correctness checks that actually fit it: the six twins get
# json_correctness + faithfulness + the deterministic estimation-correctness rubrics
# (including interval_calibration, which SKIPS on hand-authored cases lacking actuals
# and SCORES on synthetic cases carrying gold["actual_*_ml"]); the pre-/post-estimate
# agents get their own targeted checks. The self-consistency rubric (`consistency`)
# is on the twins + tooling — it re-runs the adapter N times under the runner's
# repeats knob and is a no-op skip at N=1.
_TWIN_RUBRICS: list[RubricName] = [
    "json_correctness",
    "faithfulness",
    "band_adherence",
    "algorithm_conformance",
    "role_attribution_validity",
    "estimate_accuracy",
    "interval_calibration",
    "consistency",
]


def _rubrics_for(agent: str) -> list[RubricName]:
    if agent in TWIN_AGENTS:
        return list(_TWIN_RUBRICS)
    if agent == "prefill":
        return ["summarization", "extraction_accuracy"]
    if agent == "roster":
        return ["plan_quality", "faithfulness", "staffing_adequacy"]
    if agent == "tooling":
        return ["classification_accuracy", "enum_constraint_adherence", "consistency"]
    if agent == "consolidator":
        return ["plan_quality", "partition_correctness"]
    return []


AGENT_RUBRICS: dict[str, list[RubricName]] = {
    agent: _rubrics_for(agent) for agent in ALL_AGENTS
}

# Pass thresholds. The LLM-judged rubrics (faithfulness, plan_quality,
# summarization) use 0.7. The deterministic correctness rubrics are a hard 1.0
# (any violation is a fail). estimate_accuracy / interval_calibration are banded
# relative error vs reference actuals, so they use 0.7 as a drift detector. The
# self-consistency rubric (`consistency`) measures run-to-run stability and uses 0.7
# (a noisy agent that swings widely between identical runs fails).
RUBRIC_THRESHOLDS: dict[RubricName, float] = {
    "faithfulness": 0.7,
    "plan_quality": 0.7,
    "summarization": 0.7,
    "json_correctness": 1.0,
    "band_adherence": 1.0,
    "algorithm_conformance": 1.0,
    "role_attribution_validity": 1.0,
    "estimate_accuracy": 0.7,
    "interval_calibration": 0.7,
    "extraction_accuracy": 1.0,
    "staffing_adequacy": 1.0,
    "classification_accuracy": 1.0,
    "enum_constraint_adherence": 1.0,
    "partition_correctness": 1.0,
    "consistency": 0.7,
}

# Rubrics that need MULTIPLE adapter runs of the same case to score. The runner
# re-runs each such rubric's agent ``repeats`` times and hands the rubric every
# sample (see ``runner._run_case`` + ``rubrics.score_multi``):
# - ``consistency`` scores run-to-run stability across the samples.
# - ``faithfulness`` averages its judge verdict across the samples to damp judge noise.
# At the default ``repeats == 1`` these collapse to single-sample behavior, so the
# offline tests and existing runs are unchanged.
NEEDS_MULTI_SAMPLE: frozenset[RubricName] = frozenset({"consistency", "faithfulness"})


class EvalCase(BaseModel):
    """One golden case loaded from a dataset file.

    ``input`` is agent-specific kwargs the adapter consumes (e.g. raw_input +
    a constructed parsed_context/stage2/stage3 for twins, a description for
    tooling, candidate gaps for the consolidator). ``expected_output`` is a
    concise reference describing what a good output should contain — the judges
    read it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    expected_output: str = ""
    notes: str = ""
    # Structured gold labels for the deterministic correctness rubrics (e.g.
    # prefill field targets, tooling per-phase labels, consolidator clusters, twin
    # accuracy targets). Free-form by design — each rubric reads only the keys it
    # needs. Empty for cases that carry no reference labels.
    gold: dict[str, Any] = Field(default_factory=dict)


class AgentSample(BaseModel):
    """The product of running one agent adapter on one ``EvalCase``.

    ``output_obj`` holds the raw structured Pydantic output (used by
    json_correctness), hence ``arbitrary_types_allowed``. The string fields are
    human-readable renderings the judges read.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    case_id: str
    agent: str
    task_input: str = ""
    output_text: str = ""
    output_obj: Any = None
    # Grounding context items the judges (faithfulness, plan_quality) read — the
    # discrete inputs/project-context the agent was given. NOT a retrieval step:
    # these agents don't retrieve, this is just the grounding the judge checks the
    # output's claims against.
    retrieval_context: list[str] = Field(default_factory=list)
    source_text: str | None = None
    expected_output: str = ""
    # Case gold labels (copied from EvalCase.gold) the deterministic rubrics read.
    gold: dict[str, Any] = Field(default_factory=dict)
    # Structured bits a deterministic rubric needs that aren't on output_obj —
    # e.g. phase, tooling_level, reduction_bands, roster, stage2_signals. Each
    # adapter populates the keys its rubrics consume.
    eval_context: dict[str, Any] = Field(default_factory=dict)
    is_stub: bool = False
    error: str | None = None


class RubricScore(BaseModel):
    """A single rubric's verdict for a single sample."""

    model_config = ConfigDict(extra="forbid")

    rubric: RubricName
    score: float = Field(ge=0, le=1)
    passed: bool
    reasoning: str = ""
    error: str | None = None
    # A skipped score is not applicable to this case (e.g. estimate_accuracy with
    # no reference targets). It MUST be excluded from every mean / pass-rate — it
    # is neither a pass nor a fail — so aggregation filters it out rather than
    # counting it as a 0.
    skipped: bool = False


class CaseResult(BaseModel):
    """All rubric scores for one case + the sample they were scored against."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    agent: str
    scores: list[RubricScore] = Field(default_factory=list)
    sample_error: str | None = None
    is_stub: bool = False


class AgentReport(BaseModel):
    """Per-agent aggregate: mean score + pass-rate per rubric across its cases."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    case_count: int = 0
    rubric_means: dict[str, float] = Field(default_factory=dict)
    rubric_pass_rates: dict[str, float] = Field(default_factory=dict)
    results: list[CaseResult] = Field(default_factory=list)


class EvalReport(BaseModel):
    """Top-level report across every evaluated agent."""

    model_config = ConfigDict(extra="forbid")

    judge_model: str
    agents: list[AgentReport] = Field(default_factory=list)

    @property
    def overall_pass_rate(self) -> float:
        """Fraction of (case, rubric) scores at/above their threshold.

        Skipped scores (not applicable to the case) are excluded entirely.
        """
        passes: list[bool] = [
            score.passed
            for agent in self.agents
            for result in agent.results
            for score in result.scores
            if not score.skipped
        ]
        return fmean(1.0 if p else 0.0 for p in passes) if passes else 0.0

    def rubric_means(self) -> dict[str, float]:
        """Mean score per rubric, aggregated across every agent + case.

        Skipped scores are excluded — they are not a 0, they are not applicable.
        """
        buckets: dict[str, list[float]] = {}
        for agent in self.agents:
            for result in agent.results:
                for score in result.scores:
                    if score.skipped:
                        continue
                    buckets.setdefault(score.rubric, []).append(score.score)
        return {rubric: fmean(values) for rubric, values in buckets.items() if values}

    def failing_rubrics(self) -> dict[str, float]:
        """Rubrics whose overall mean is below their threshold (CI gate)."""
        means = self.rubric_means()
        return {
            rubric: mean
            for rubric, mean in means.items()
            if mean < RUBRIC_THRESHOLDS[rubric]  # type: ignore[index]
        }

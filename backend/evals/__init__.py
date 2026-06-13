"""LLM-evaluation harness.

A custom Claude-as-judge harness (no deepeval/ragas/promptfoo) that evaluates
every LLM agent in the estimator against five rubric judges, reusing the existing
``orchestrator.llm.call_structured`` plumbing for all judge calls.

Entry points:
- ``evals.runner.run_evals`` — programmatic batch run.
- ``python -m evals.run`` — CLI (mirrors ``orchestrator.smoke``).
"""

from __future__ import annotations

from .models import (
    AGENT_RUBRICS,
    ALL_AGENTS,
    RUBRIC_THRESHOLDS,
    TWIN_AGENTS,
    AgentReport,
    AgentSample,
    CaseResult,
    EvalCase,
    EvalReport,
    RubricName,
    RubricScore,
)

__all__ = [
    "AGENT_RUBRICS",
    "ALL_AGENTS",
    "RUBRIC_THRESHOLDS",
    "TWIN_AGENTS",
    "AgentReport",
    "AgentSample",
    "CaseResult",
    "EvalCase",
    "EvalReport",
    "RubricName",
    "RubricScore",
]

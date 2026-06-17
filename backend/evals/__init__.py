"""LLM-evaluation harness.

A custom LLM-as-judge harness (no deepeval/ragas/promptfoo) that evaluates every LLM
agent in the estimator against a mix of deterministic and LLM-judged rubrics. The
LLM judge defaults to OpenAI GPT-5.5 (``evals.judge.judge_structured``) — a different
provider from the Anthropic twins it grades — and falls back to
``orchestrator.llm.call_structured`` when pointed at a ``claude-*`` model.

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

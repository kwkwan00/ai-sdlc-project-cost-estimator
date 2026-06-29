"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _block_real_openai_calls(monkeypatch):
    """Never let a test hit the real OpenAI API. The WBS planner + completeness critic default to
    GPT-5.5 (``config.wbs_model``), routed through ``orchestrator.llm._call_structured_openai`` →
    ``_get_openai_client``. Without this guard, a developer with ``OPENAI_API_KEY`` set would fire real
    billable GPT-5.5 calls in the planner/completeness degradation tests (the suite is otherwise run
    with the API keys blanked, but that command doesn't unset OPENAI_API_KEY).

    Forces the OpenAI client getter to raise, so those paths degrade exactly as they do with no key.
    Tests that exercise the OpenAI path mock ``orchestrator.llm._get_openai_client`` in their own body,
    which (same function-scoped ``monkeypatch``) overrides this for that test. The eval judge keeps its
    own binding (``evals.judge._get_openai_client``) and is unaffected."""
    import orchestrator.llm as llm

    def _no_openai_client():
        raise RuntimeError("OPENAI_API_KEY is not set")

    monkeypatch.setattr(llm, "_get_openai_client", _no_openai_client, raising=False)
    yield

"""Coverage for the parse_input short-circuit branch.

The branch lets callers (smoke harness, tests, integration tests) pre-fill
parsed_context to skip the Anthropic call. Verified here without any
LLM mocking — if the short-circuit works, no network call is even attempted.
"""

from __future__ import annotations

import pytest

from orchestrator.nodes.parse_input import parse_input


@pytest.mark.asyncio
async def test_parse_input_short_circuits_when_context_pre_filled() -> None:
    state = {
        "raw_input": "Build a thing.",
        "parsed_context": {"industry_hint": "fintech", "summary": "pre-supplied"},
    }
    result = await parse_input(state)
    # Returns the pre-supplied context unchanged.
    assert result["parsed_context"] == state["parsed_context"]


@pytest.mark.asyncio
async def test_parse_input_short_circuits_on_truthy_dict_with_data() -> None:
    state = {"raw_input": "x", "parsed_context": {"summary": "x"}}
    result = await parse_input(state)
    assert result["parsed_context"]["summary"] == "x"


@pytest.mark.asyncio
async def test_parse_input_treats_empty_dict_as_unfilled(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty parsed_context should NOT short-circuit; the LLM call should be attempted."""
    called = {"n": 0}

    async def fake_call_structured(**kwargs):
        called["n"] += 1
        # Mimic the shape Claude would return.
        from orchestrator.nodes.parse_input import ParsedContext

        return ParsedContext(summary="from llm", ambiguity_score=0.3)

    monkeypatch.setattr(
        "orchestrator.nodes.parse_input.call_structured", fake_call_structured
    )

    state = {"raw_input": "Build a thing.", "parsed_context": {}}
    result = await parse_input(state)
    assert called["n"] == 1
    assert result["parsed_context"]["summary"] == "from llm"


@pytest.mark.asyncio
async def test_parse_input_attaches_calibration_examples_when_repo_returns_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_calibration(**kwargs):
        return {
            "discovery": [{"phase": "discovery", "sample_count": 5}],
            "ux_design": [],
            "development": [{"phase": "development", "sample_count": 3}],
            "code_review": [],
            "deployment": [],
            "qa_testing": [],
        }

    monkeypatch.setattr(
        "orchestrator.nodes.parse_input.get_calibration_for_all_phases",
        fake_calibration,
    )

    state = {
        "raw_input": "Build a thing.",
        "parsed_context": {"industry_hint": "fintech", "project_type_hint": "greenfield"},
    }
    result = await parse_input(state)
    flat = result["calibration_examples"]
    phases = {row["phase"] for row in flat}
    assert phases == {"discovery", "development"}


@pytest.mark.asyncio
async def test_parse_input_returns_empty_calibration_when_repo_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(**kwargs):
        raise RuntimeError("postgres unreachable")

    monkeypatch.setattr(
        "orchestrator.nodes.parse_input.get_calibration_for_all_phases", boom
    )
    state = {
        "raw_input": "x",
        "parsed_context": {"summary": "preset"},
    }
    result = await parse_input(state)
    assert result["calibration_examples"] == []


@pytest.mark.asyncio
async def test_parse_input_falls_back_to_stub_context_when_llm_call_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ANTHROPIC_API_KEY, parse_input must NOT crash the graph — it
    should return a minimal fallback context so downstream twins (themselves
    stub-fallback aware) can still run."""

    async def boom(**_kwargs):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr("orchestrator.nodes.parse_input.call_structured", boom)

    raw = "Build a HIPAA-compliant patient portal with appointments and messaging."
    result = await parse_input({"raw_input": raw, "parsed_context": {}})

    assert "parsed_context" in result
    ctx = result["parsed_context"]
    # Fallback summarizes the raw input (first 280 chars).
    assert raw.startswith(ctx["summary"][:50])
    # Conservative ambiguity score when we couldn't actually parse.
    assert ctx["ambiguity_score"] >= 0.5

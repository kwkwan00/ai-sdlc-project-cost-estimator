"""End-to-end graph compile + interrupt-resume cycle, with parse_input patched.

Verifies the graph wiring without requiring an Anthropic API key:
- All six Pass-1 twins run in parallel and fan into merge_pass1
- The graph interrupts at await_user_answers (Stage 4)
- Resuming with Command(resume=...) runs Pass-2 twins and reaches synthesize
"""

from __future__ import annotations

import pytest
from langgraph.types import Command

from models.project_schema import Stage2Context, Stage3Maturity


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch):
    """Compile the graph with parse_input's LLM call stubbed out."""

    async def fake_parse(state):
        return {
            "parsed_context": {
                "industry_hint": "healthcare",
                "project_type_hint": "greenfield",
                "screen_count_estimate": 25,
                "summary": "Patient portal",
                "ambiguity_score": 0.4,
                "integration_mentions": ["Epic", "Stripe"],
                "regulatory_mentions": ["HIPAA"],
                "tech_stack_mentions": [],
                "user_role_mentions": ["patient", "provider"],
                "ai_feature_mentions": [],
            }
        }

    # Patch the symbol both where it's defined and where graph.py imported it.
    monkeypatch.setattr("orchestrator.nodes.parse_input.parse_input", fake_parse)
    monkeypatch.setattr("orchestrator.graph.parse_input", fake_parse)

    # Force every twin onto its deterministic stub path. The twins wrap
    # call_structured in try/except -> stub_phase_estimate, so disabling the LLM
    # client makes the graph run offline with stable stub numbers. Without this
    # the test would make six live, non-deterministic twin calls whenever
    # ANTHROPIC_API_KEY happens to be set, breaking the stub-based assertions
    # below (e.g. manual_only > ai_assisted).
    def _no_llm_client():
        raise RuntimeError("LLM disabled for graph wiring test")

    monkeypatch.setattr("orchestrator.llm._get_client", _no_llm_client)

    from orchestrator.graph import build_graph

    return build_graph(with_checkpointer=True)


@pytest.mark.asyncio
async def test_graph_compiles_and_interrupts_at_stage4(graph) -> None:
    config = {"configurable": {"thread_id": "test-1"}}
    initial = {
        "estimate_id": "test-1",
        "project_name": "Patient portal",
        "raw_input": "Build a HIPAA-compliant patient portal.",
        "stage2": Stage2Context(industry="healthcare", target_timeline_weeks=20),
        "stage3": Stage3Maturity(),
        "parsed_context": {},
    }

    result = await graph.ainvoke(initial, config=config)

    # Pass 1 must have produced six phase estimates (one per twin).
    assert len(result.get("pass1_estimates", [])) == 6
    phases = {p.phase for p in result["pass1_estimates"]}
    assert len(phases) == 6  # all distinct phases

    # The graph paused for clarifying questions — interrupt key is present.
    assert "__interrupt__" in result


@pytest.mark.asyncio
async def test_graph_resumes_to_final_estimate(graph) -> None:
    config = {"configurable": {"thread_id": "test-2"}}
    initial = {
        "estimate_id": "test-2",
        "project_name": "Patient portal",
        "raw_input": "Build a HIPAA-compliant patient portal.",
        "stage2": Stage2Context(industry="healthcare", target_timeline_weeks=20),
        "stage3": Stage3Maturity(),
        "parsed_context": {},
    }
    await graph.ainvoke(initial, config=config)

    final = await graph.ainvoke(Command(resume={"answers": {}}), config=config)

    assert "final_estimate" in final
    fe = final["final_estimate"]
    assert fe is not None
    assert len(fe.phases) == 6
    # Stub estimates are all positive.
    assert fe.total_ai_assisted_hours.most_likely > 0
    assert fe.total_manual_only_hours.most_likely > fe.total_ai_assisted_hours.most_likely

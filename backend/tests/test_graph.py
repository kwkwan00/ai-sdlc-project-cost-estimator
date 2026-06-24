"""End-to-end graph compile + interrupt-resume cycle, with parse_input patched.

Verifies the graph wiring without requiring an Anthropic API key:
- All six Pass-1 twins run in parallel and fan into merge_pass1
- The graph interrupts at await_user_answers (Stage 4)
- Resuming with Command(resume=...) runs Pass-2 twins and reaches synthesize
"""

from __future__ import annotations

import pytest
from langgraph.types import Command

from models.project_schema import Stage2Context, Stage3Context
from models.twin_outputs import Phase


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
        "stage3": Stage3Context(),
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
        "stage3": Stage3Context(),
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


@pytest.mark.asyncio
async def test_graph_runs_only_selected_phases(graph) -> None:
    """selected_phases restricts which twins contribute — the others return {} (no LLM call),
    and synthesize rolls up only the chosen phases."""
    config = {"configurable": {"thread_id": "test-subset"}}
    initial = {
        "estimate_id": "test-subset",
        "project_name": "Patient portal",
        "raw_input": "Build a HIPAA-compliant patient portal.",
        "stage2": Stage2Context(industry="healthcare", target_timeline_weeks=20),
        "stage3": Stage3Context(),
        "parsed_context": {},
        "selected_phases": [Phase.DEVELOPMENT, Phase.QA_TESTING],
    }

    result = await graph.ainvoke(initial, config=config)
    # Only the two selected twins appended a Pass-1 estimate; the other four returned {}.
    assert {p.phase for p in result["pass1_estimates"]} == {Phase.DEVELOPMENT, Phase.QA_TESTING}

    final = await graph.ainvoke(Command(resume={"answers": {}}), config=config)
    fe = final["final_estimate"]
    assert fe is not None
    assert {p.phase for p in fe.phases} == {Phase.DEVELOPMENT, Phase.QA_TESTING}
    # Project total == Σ of just the selected phases' mids (the load-bearing invariant survives).
    assert fe.total_ai_assisted_hours.most_likely == pytest.approx(
        sum(p.ai_assisted_hours.most_likely for p in fe.phases)
    )
    assert fe.total_manual_only_hours.most_likely > fe.total_ai_assisted_hours.most_likely


@pytest.mark.asyncio
async def test_graph_single_phase_subset_synthesizes_cleanly(graph) -> None:
    """The degenerate single-phase case must reach a finite, positive final estimate end-to-end.

    NOTE: the graph fixture forces every twin onto its stub path (``HourRange.std is None``), so
    this exercises synthesize's comonotonic combine + the staffing model for one phase. The
    single-phase *variance-combine* (lognormal) branch is covered directly by
    ``test_phase_selection.test_combine_range_single_phase_variance_path_is_finite``."""
    config = {"configurable": {"thread_id": "test-single"}}
    initial = {
        "estimate_id": "test-single",
        "project_name": "Patient portal",
        "raw_input": "Build a HIPAA-compliant patient portal.",
        "stage2": Stage2Context(industry="healthcare", target_timeline_weeks=20),
        "stage3": Stage3Context(),
        "parsed_context": {},
        "selected_phases": [Phase.DEVELOPMENT],
    }
    await graph.ainvoke(initial, config=config)
    final = await graph.ainvoke(Command(resume={"answers": {}}), config=config)

    fe = final["final_estimate"]
    assert fe is not None
    assert {p.phase for p in fe.phases} == {Phase.DEVELOPMENT}
    assert fe.total_ai_assisted_hours.most_likely > 0
    assert fe.team_size >= 1

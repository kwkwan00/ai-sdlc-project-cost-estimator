"""Coverage for the streaming WBS planner: the package-name stream parser, the delta→callback
wiring, and the AG-UI endpoint lifecycle (POST /wbs/draft/agui).

The parser is exercised directly (it's pure); the endpoint is driven through FastAPI's TestClient
with the planner stubbed, asserting RUN_STARTED → CUSTOM(per package) → STATE_SNAPSHOT → RUN_FINISHED
and the never-fail degrade to a skeleton when the planner is unavailable.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agents.wbs_agent import (
    WbsPlannerLeaf,
    WbsPlannerPackage,
    WbsPlannerResponse,
    _PackageNameStreamParser,
    run_wbs_planner_streamed,
)
from models.wbs_schema import WbsDraftRequest


@pytest.fixture
def client(monkeypatch) -> TestClient:
    import db.postgres_adapter as pg

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)
    monkeypatch.setenv("MC_DRAWS", "200")
    from main import app

    return TestClient(app)


# --- the pure package-name stream parser ----------------------------------------------------


def _feed_all(chunks: list[str]) -> list[str]:
    parser = _PackageNameStreamParser()
    out: list[tuple[str, str]] = []
    for c in chunks:
        out.extend(parser.feed(c))
    return out


def test_parser_tags_packages_and_tasks_by_kind() -> None:
    blob = (
        '{"packages":['
        '{"name":"Discovery","tasks":[{"name":"Interviews"},{"name":"Workshops"}]},'
        '{"name":"Build","tasks":[{"name":"API"}]}'
        '],"notes":"ok"}'
    )
    # Packages (depth 3) and tasks (depth 5) are emitted in document order, each tagged by kind.
    assert _feed_all([blob]) == [
        ("package", "Discovery"),
        ("task", "Interviews"),
        ("task", "Workshops"),
        ("package", "Build"),
        ("task", "API"),
    ]


def test_parser_is_robust_to_chunk_boundaries() -> None:
    blob = '{"packages":[{"name":"Discovery & Setup","tasks":[]},{"name":"Core Build","tasks":[]}]}'
    # Char-by-char feeding must yield exactly the same events (state survives across feed() calls).
    assert _feed_all(list(blob)) == [
        ("package", "Discovery & Setup"),
        ("package", "Core Build"),
    ]


def test_parser_handles_name_as_last_field_and_escapes() -> None:
    blob = r'{"packages":[{"tasks":[],"name":"Build \"v2\""},{"name":"Ship"}]}'
    # A `}`-terminated value still emits, and JSON escapes are decoded.
    assert _feed_all([blob]) == [("package", 'Build "v2"'), ("package", "Ship")]


def test_parser_emits_nothing_for_empty_or_taskless_shapes() -> None:
    assert _feed_all(['{"packages":[],"notes":"none"}']) == []
    assert _feed_all([""]) == []
    # A top-level `notes` string is at depth 1, never mistaken for a package/task name.
    assert _feed_all(['{"notes":"name is here","packages":[]}']) == []


# --- delta → parser → on_node wiring (run_wbs_planner_streamed) ------------------------------


async def test_run_wbs_planner_streamed_fires_on_node_per_package_and_task(monkeypatch) -> None:
    blob = (
        '{"packages":[{"name":"Discovery","tasks":[{"name":"Interviews"}]},'
        '{"name":"Build","tasks":[]}],"notes":"ok"}'
    )

    captured: dict = {}

    async def fake_stream(*, on_input_delta=None, **kwargs: object) -> WbsPlannerResponse:
        captured.update(kwargs)
        if on_input_delta is not None:
            # Split mid-stream to exercise the parser's cross-chunk state through the real wiring.
            on_input_delta(blob[:40])
            on_input_delta(blob[40:])
        return WbsPlannerResponse(
            packages=[
                WbsPlannerPackage(name="Discovery", tasks=[WbsPlannerLeaf(name="Interviews")]),
                WbsPlannerPackage(name="Build", tasks=[WbsPlannerLeaf(name="y")]),
            ],
            notes="ok",
        )

    monkeypatch.setattr("agents.wbs_agent.stream_structured", fake_stream)
    seen: list[tuple[str, str]] = []
    req = WbsDraftRequest(raw_input="A small internal tool for ticket triage and reporting.")
    resp = await run_wbs_planner_streamed(req, on_node=lambda kind, name: seen.append((kind, name)))
    # Packages + tasks stream through on_node in document order (Build has no tasks → no task event).
    assert seen == [("package", "Discovery"), ("task", "Interviews"), ("package", "Build")]
    assert [p.name for p in resp.packages] == ["Discovery", "Build"]
    # The streaming planner forwards the configured WBS model + effort (Opus 4.8 / max by default),
    # so the streamed draft runs at the same reasoning depth as the non-streaming fallback.
    assert captured["model"] == "claude-opus-4-8"
    assert captured["effort"] == "max"
    # A generous max_tokens so max-effort reasoning + the full tree both fit (reasoning shares the
    # output-token envelope). Guards against regressing to a budget that truncates to the skeleton.
    assert captured["max_tokens"] >= 32000


# --- AG-UI endpoint -------------------------------------------------------------------------


def _run_input() -> dict:
    # camelCase, as the @ag-ui/client HttpAgent sends it.
    return {
        "threadId": "t1",
        "runId": "r1",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "raw_input": "A patient scheduling portal for a regional clinic with HL7 integration.",
            "project_name": "Clinic Portal",
            "selected_phases": ["development", "qa_testing"],
        },
    }


def _parse_sse_events(body: str) -> list[dict]:
    events: list[dict] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):].strip()))
    return events


def test_extract_wbs_request_parses_forwarded_props() -> None:
    from ag_ui.core import RunAgentInput

    from models.twin_outputs import Phase
    from routers.wbs import _extract_wbs_request

    req = _extract_wbs_request(RunAgentInput.model_validate(_run_input()))
    assert req.raw_input.startswith("A patient scheduling")
    assert req.project_name == "Clinic Portal"
    assert req.selected_phases == [Phase.DEVELOPMENT, Phase.QA_TESTING]


def test_wbs_agui_streams_friendly_progress_then_snapshot(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    from models.twin_outputs import Phase
    from models.wbs_task import WbsTaskInput

    async def fake_streamed(req, *, on_node):  # noqa: ANN001, ANN202
        on_node("package", "Discovery & Setup")
        on_node("task", "Stakeholder interviews")
        on_node("package", "Core Build")
        tree = [
            WbsTaskInput(
                id="p1",
                name="Discovery & Setup",
                children=[
                    WbsTaskInput(
                        id="l1", name="Interviews", phase=Phase.DISCOVERY,
                        role_id="sr_engineer", optimistic=1, most_likely=2, pessimistic=3,
                    )
                ],
            )
        ]
        return tree, "drafted notes"

    monkeypatch.setattr("routers.wbs.generate_wbs_tree_streamed", fake_streamed)

    res = client.post("/wbs/draft/agui", json=_run_input())
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse_events(res.text)
    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    assert "STATE_SNAPSHOT" in types

    # Every CUSTOM event is a human-readable wbs_progress message (only the latest shown by the UI).
    customs = [e for e in events if e["type"] == "CUSTOM"]
    assert all(e["name"] == "wbs_progress" for e in customs)
    messages = [e["value"]["message"] for e in customs]
    assert any("Reviewing" in m for m in messages)  # opening milestone
    assert any("Planning work package 1: Discovery & Setup" in m for m in messages)  # package
    assert any("Adding task to Discovery & Setup: Stakeholder interviews" in m for m in messages)
    assert any("Planning work package 2: Core Build" in m for m in messages)
    assert any("finalizing" in m.lower() for m in messages)  # closing milestone
    # Milestones bracket the node messages: "Reviewing…" first, "…finalizing…" last.
    assert "Reviewing" in messages[0] and "finalizing" in messages[-1].lower()

    snapshot = next(e for e in events if e["type"] == "STATE_SNAPSHOT")["snapshot"]
    assert snapshot["draft_id"]
    assert snapshot["tree"][0]["name"] == "Discovery & Setup"
    assert snapshot["notes"] == "drafted notes"


def test_wbs_agui_degrades_to_skeleton_when_planner_unavailable(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    # Force the LLM client unavailable (mirrors no API key) so BOTH the streaming path and its
    # non-streaming fallback degrade. The endpoint must still deliver a valid skeleton snapshot and
    # never RUN_ERROR — and only the milestone messages fire, no per-package/task narration.
    def _no_client(*_a: object, **_k: object) -> object:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr("orchestrator.llm._get_client", _no_client)

    res = client.post("/wbs/draft/agui", json=_run_input())
    assert res.status_code == 200
    events = _parse_sse_events(res.text)
    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    assert "STATE_SNAPSHOT" in types
    assert "RUN_ERROR" not in types

    messages = [e["value"]["message"] for e in events if e["type"] == "CUSTOM"]
    # Only the deterministic milestones — no per-package/task narration on the fallback path.
    assert messages
    assert all("work package" not in m.lower() and "adding task" not in m.lower() for m in messages)

    snapshot = next(e for e in events if e["type"] == "STATE_SNAPSHOT")["snapshot"]
    assert snapshot["draft_id"]
    assert len(snapshot["tree"]) >= 1  # the skeleton always has at least one package

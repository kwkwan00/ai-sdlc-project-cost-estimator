"""Per-leaf hour estimator (#5c) — the editor's "Suggest hours" button."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.wbs_leaf_estimator import (
    _find_leaf_context,
    _LeafHoursReply,
    suggest_leaf_hours,
)
from models.twin_outputs import Phase
from models.wbs_schema import WbsLeafHoursRequest
from models.wbs_task import WbsTaskInput


def _tree() -> list[WbsTaskInput]:
    return [
        WbsTaskInput(id="pkg", name="Auth", children=[
            WbsTaskInput(id="l1", name="Login API", phase=Phase.DEVELOPMENT, role_id="r",
                         optimistic=8, most_likely=16, pessimistic=32),
            WbsTaskInput(id="l2", name="Password reset", phase=Phase.DEVELOPMENT, role_id="r",
                         optimistic=4, most_likely=8, pessimistic=16),
        ]),
    ]


def test_find_leaf_context_returns_package_and_siblings() -> None:
    leaf, package, siblings = _find_leaf_context(_tree(), "l1")
    assert leaf is not None and leaf.name == "Login API"
    assert package == "Auth"  # the work package the leaf lives under
    # Siblings carry their current hours so the model can size proportionately.
    assert siblings == [{"name": "Password reset", "most_likely_hours": 8.0}]


def test_find_leaf_context_missing_id_returns_none() -> None:
    leaf, package, siblings = _find_leaf_context(_tree(), "nope")
    assert leaf is None and package == "" and siblings == []
    # A branch id is not a leaf → also no context.
    assert _find_leaf_context(_tree(), "pkg")[0] is None


def test_leaf_hours_reply_coerces_pert_ordering() -> None:
    # A malformed 3-point reply is repaired (min/mid/max), not rejected.
    r = _LeafHoursReply(optimistic=40, most_likely=10, pessimistic=20)
    assert (r.optimistic, r.most_likely, r.pessimistic) == (10, 20, 40)


@pytest.mark.asyncio
async def test_suggest_leaf_hours_missing_leaf_is_unavailable() -> None:
    resp = await suggest_leaf_hours(WbsLeafHoursRequest(raw_input="x", tree=_tree(), leaf_id="nope"))
    assert resp.available is False


@pytest.mark.asyncio
async def test_suggest_leaf_hours_degrades_without_api_key() -> None:
    # No ANTHROPIC_API_KEY in tests → call_structured raises → available=False, never an error.
    resp = await suggest_leaf_hours(
        WbsLeafHoursRequest(raw_input="Build a HIPAA portal.", tree=_tree(), leaf_id="l1")
    )
    assert resp.available is False
    assert resp.llm_usage is None


@pytest.mark.asyncio
async def test_suggest_leaf_hours_maps_reply_and_uses_configured_model(monkeypatch) -> None:
    # Mock the LLM → the suggestion surfaces the reply AND uses the configured WBS model + effort
    # (Claude Opus 4.8 / max by default), with a generous max_tokens budget.
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _LeafHoursReply(optimistic=12, most_likely=24, pessimistic=48,
                               rationale="Third-party API with auth + error handling.")

    monkeypatch.setattr("agents.wbs_leaf_estimator.call_structured", _fake)
    resp = await suggest_leaf_hours(
        WbsLeafHoursRequest(raw_input="HIPAA portal", tree=_tree(), leaf_id="l1")
    )
    assert captured["model"] == "claude-opus-4-8"
    assert captured["effort"] == "max"
    assert captured["max_tokens"] >= 4096
    assert resp.available is True
    assert (resp.optimistic, resp.most_likely, resp.pessimistic) == (12, 24, 48)
    assert resp.rationale.startswith("Third-party API")


@pytest.mark.asyncio
async def test_suggest_leaf_hours_feeds_similar_past_tasks_into_prompt(monkeypatch) -> None:
    # RAG: nearest_wbs_tasks (Qdrant) supplies realized hours of similar past tasks, which must reach
    # the LLM prompt as `similar_past_tasks` anchors, scoped to the leaf's phase.
    captured: dict = {}
    seen_query: dict = {}

    async def _fake_nearest(query_text, *, limit=5, phase=None):
        seen_query["text"] = query_text
        seen_query["phase"] = phase
        return [
            {"task_name": "Login API", "phase": "development", "role_id": "r",
             "optimistic": 10.0, "most_likely": 20.0, "pessimistic": 40.0, "score": 0.91},
        ]

    async def _fake_call(**kwargs):
        captured.update(kwargs)
        return _LeafHoursReply(optimistic=12, most_likely=22, pessimistic=44, rationale="anchored")

    monkeypatch.setattr("agents.wbs_leaf_estimator.nearest_wbs_tasks", _fake_nearest)
    monkeypatch.setattr("agents.wbs_leaf_estimator.call_structured", _fake_call)

    resp = await suggest_leaf_hours(
        WbsLeafHoursRequest(raw_input="HIPAA portal", tree=_tree(), leaf_id="l1")
    )
    assert resp.available is True
    # The retrieval query is the leaf's embedding text, scoped to the leaf's phase.
    assert "Login API" in seen_query["text"]
    assert seen_query["phase"] is Phase.DEVELOPMENT
    # The retrieved anchor (name + realized hours + similarity) is rendered into the prompt.
    user = captured["user"]
    assert "similar_past_tasks" in user
    assert "Login API" in user and "20.0" in user and "0.91" in user


@pytest.fixture
def client(monkeypatch) -> TestClient:
    import db.postgres_adapter as pg

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)
    from main import app

    return TestClient(app)


def test_suggest_hours_endpoint_returns_valid_shape(client: TestClient) -> None:
    body = {
        "raw_input": "Build a HIPAA patient portal.",
        "leaf_id": "l1",
        "tree": [{"id": "pkg", "name": "Auth", "children": [
            {"id": "l1", "name": "Login API", "phase": "development", "role_id": "r",
             "optimistic": 8, "most_likely": 16, "pessimistic": 32},
        ]}],
    }
    with client as c:
        r = c.post("/estimates/wbs/suggest-hours", json=body)
    assert r.status_code == 200
    out = r.json()
    # No API key on the stub path → available=False, but a valid, applied-only-when-true shape.
    assert out["available"] is False
    assert {"optimistic", "most_likely", "pessimistic", "rationale"} <= out.keys()


def test_suggest_hours_endpoint_persists_usage_to_llm_call(client: TestClient, monkeypatch) -> None:
    # The suggestion's token cost is BOTH returned AND persisted to `llm_call` (global Observability),
    # stamped with the wizard `session_id` for reparenting onto the estimate on commit.
    import db.repositories as repos
    from orchestrator import usage

    async def _fake_call(**kwargs):
        usage.record_usage(model="claude-opus-4-8", input_tokens=80, output_tokens=30,
                           cache_read_tokens=0, agent="suggest_leaf_hours")
        return _LeafHoursReply(optimistic=12, most_likely=24, pessimistic=48, rationale="ok")

    captured: dict = {}

    async def _fake_insert(rows, *, estimate_id=None, session_id=None):
        captured["rows"] = rows
        captured["session_id"] = session_id

    monkeypatch.setattr("agents.wbs_leaf_estimator.call_structured", _fake_call)
    monkeypatch.setattr(repos, "insert_llm_calls", _fake_insert)

    body = {
        "raw_input": "HIPAA portal", "leaf_id": "l1", "session_id": "wiz-5",
        "tree": [{"id": "pkg", "name": "Auth", "children": [
            {"id": "l1", "name": "Login API", "phase": "development", "role_id": "r",
             "optimistic": 8, "most_likely": 16, "pessimistic": 32},
        ]}],
    }
    with client as c:
        r = c.post("/estimates/wbs/suggest-hours", json=body)
    assert r.status_code == 200
    out = r.json()
    assert out["available"] is True
    assert out["llm_usage"] is not None
    assert captured["session_id"] == "wiz-5"
    assert len(captured["rows"]) == 1
    assert captured["rows"][0]["agent"] == "suggest_leaf_hours"

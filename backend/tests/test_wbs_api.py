"""WBS HTTP surface: draft, preview, commit, duplicate — exercised with Neo4j + Postgres off.

With both persistence layers disabled, drafts degrade (empty resume list, 404 on load) but the
in-memory compute + commit paths still work end to end, which is what these assert.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch) -> TestClient:
    import db.postgres_adapter as pg

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)
    monkeypatch.setenv("MC_DRAWS", "200")
    from main import app

    return TestClient(app)


def _leaf(tid: str, role: str = "sr_engineer") -> dict:
    return {
        "id": tid, "name": tid, "phase": "development", "role_id": role,
        "optimistic": 8, "most_likely": 16, "pessimistic": 32,
    }


def _tree() -> list[dict]:
    return [{"id": "p1", "name": "Build", "children": [_leaf("l1"), _leaf("l2", "jr_engineer")]}]


def test_draft_wbs_returns_tree(client: TestClient) -> None:
    with client as c:
        r = c.post("/wbs/draft", json={"raw_input": "Build a small expense tracker app."})
    assert r.status_code == 200
    body = r.json()
    assert body["draft_id"]
    assert body["tree"], "fallback skeleton must yield a non-empty tree"


def test_calculate_wbs_creates_envelope(client: TestClient) -> None:
    with client as c:
        r = c.post("/estimates/wbs", json={"project_name": "CRM", "tree": _tree()})
        assert r.status_code == 200
        env = r.json()
        assert env["method"] == "wbs"
        assert env["final_estimate"] is not None
        assert env["wbs_tree"]
        # fetchable via the shared GET endpoint
        got = c.get(f"/estimates/{env['estimate_id']}")
        assert got.status_code == 200
        assert got.json()["method"] == "wbs"


def test_preview_wbs_returns_estimate_without_persisting(client: TestClient) -> None:
    with client as c:
        r = c.post("/estimates/wbs/preview", json={"tree": _tree()})
    assert r.status_code == 200
    body = r.json()
    # It's a DualScenarioEstimate, not an envelope (no estimate_id).
    assert "total_ai_assisted_hours" in body
    assert "estimate_id" not in body


def test_preview_and_commit_same_tree_yield_same_numbers(client: TestClient) -> None:
    # The Monte Carlo bands are seeded from a STABLE per-tree value (not the ephemeral/fresh
    # estimate_id), so what the user sees in Re-evaluate (preview) must equal what gets SAVED
    # on commit for the same tree — including the MC band edges + percentiles, not just the mid.
    tree = _tree()
    with client as c:
        prev = c.post("/estimates/wbs/preview", json={"tree": tree}).json()
        committed = c.post("/estimates/wbs", json={"project_name": "Seed", "tree": tree}).json()
    final = committed["final_estimate"]
    for key in ("total_ai_assisted_hours", "total_manual_only_hours"):
        assert prev[key] == final[key], f"{key} diverged between preview and commit"


def test_preview_is_deterministic_across_calls(client: TestClient) -> None:
    tree = _tree()
    with client as c:
        a = c.post("/estimates/wbs/preview", json={"tree": tree}).json()
        b = c.post("/estimates/wbs/preview", json={"tree": tree}).json()
    assert a["total_ai_assisted_hours"] == b["total_ai_assisted_hours"]
    assert a["total_manual_only_hours"] == b["total_manual_only_hours"]


def test_draft_id_drives_the_seed_not_tree_identity(client: TestClient) -> None:
    # When a draft_id is present it pins the seed; preview and commit of that draft match.
    tree = _tree()
    with client as c:
        prev = c.post(
            "/estimates/wbs/preview", json={"draft_id": "draft-xyz", "tree": tree}
        ).json()
        committed = c.post(
            "/estimates/wbs", json={"draft_id": "draft-xyz", "project_name": "D", "tree": tree}
        ).json()
    assert prev["total_ai_assisted_hours"] == committed["final_estimate"]["total_ai_assisted_hours"]


def test_branch_carrying_estimate_fields_is_422(client: TestClient) -> None:
    bad_tree = [
        {
            "id": "b", "name": "b", "phase": "development",  # branch must NOT carry phase
            "children": [_leaf("l1")],
        }
    ]
    with client as c:
        r = c.post("/estimates/wbs", json={"tree": bad_tree})
    assert r.status_code == 422


def test_duplicate_from_completed_estimate(client: TestClient) -> None:
    with client as c:
        created = c.post("/estimates/wbs", json={"project_name": "Orig", "tree": _tree()}).json()
        est_id = created["estimate_id"]
        r = c.post(f"/estimates/{est_id}/wbs/duplicate")
        assert r.status_code == 200
        dup = r.json()
    assert dup["draft_id"] != est_id
    # task ids regenerated → disjoint from the source tree's ids
    src_ids = {"p1", "l1", "l2"}
    dup_ids = set()

    def _collect(nodes: list[dict]) -> None:
        for n in nodes:
            dup_ids.add(n["id"])
            _collect(n.get("children", []))

    _collect(dup["tree"])
    assert src_ids.isdisjoint(dup_ids)


def test_copy_name_does_not_stack_marker() -> None:
    from routers.wbs import _copy_name

    assert _copy_name("Orders") == "Orders (Copy)"
    assert _copy_name("Orders (Copy)") == "Orders (Copy)"  # re-duplicating doesn't stack
    assert _copy_name("") == "WBS draft (Copy)"


def test_duplicate_non_wbs_estimate_is_409(client: TestClient) -> None:
    import runtime
    from models.project_schema import EstimateEnvelope, EstimateStatus

    env = EstimateEnvelope(
        estimate_id="twin-1", project_name="A twin estimate",
        status=EstimateStatus.COMPLETED, created_at=datetime.now(UTC),
    )  # method defaults to "twins"
    runtime._envelopes["twin-1"] = env
    try:
        with client as c:
            r = c.post("/estimates/twin-1/wbs/duplicate")
        assert r.status_code == 409
    finally:
        runtime._envelopes.pop("twin-1", None)


def test_stable_seed_prefers_draft_id_and_is_content_stable() -> None:
    from models.wbs_schema import WbsCalculateRequest
    from routers.wbs import _stable_seed

    tree = _tree()
    # draft_id pins the seed regardless of tree content.
    s_draft = _stable_seed(WbsCalculateRequest(draft_id="abc", tree=tree))
    assert s_draft == _stable_seed(
        WbsCalculateRequest(draft_id="abc", tree=[{"id": "z", "name": "z", "children": [_leaf("x")]}])
    )
    # No draft_id → content hash; identical trees hash the same, different trees differ.
    s1 = _stable_seed(WbsCalculateRequest(tree=tree))
    s2 = _stable_seed(WbsCalculateRequest(tree=_tree()))
    assert s1 == s2
    other = [{"id": "p1", "name": "Build", "children": [_leaf("l1")]}]
    assert _stable_seed(WbsCalculateRequest(tree=other)) != s1


def test_draft_list_and_load_degrade_when_neo4j_off(client: TestClient) -> None:
    with client as c:
        listing = c.get("/wbs/drafts")
        assert listing.status_code == 200
        body = listing.json()
        assert body["items"] == []
        assert body["resumable"] is False  # no Neo4j driver in tests
        # load of any id → 404 (client then falls back to its localStorage cache)
        assert c.get("/wbs/drafts/whatever").status_code == 404

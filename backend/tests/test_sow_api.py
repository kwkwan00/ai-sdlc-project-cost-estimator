"""HTTP tests for the SOW endpoints. The agent is monkeypatched (no real LLM call)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

import runtime
from main import app
from models.project_schema import EstimateEnvelope, EstimateStatus
from sow import composer as composer_mod
from sow.models import SowClientFacts

from ._sow_fixtures import make_completed_envelope

_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _stub_agent(monkeypatch) -> None:
    async def _fake_generate(template, envelope, scenario):
        return {s.id: f"{s.id} prose" for s in template.sections if s.source == "llm"}, SowClientFacts()

    monkeypatch.setattr(composer_mod, "generate_prose", _fake_generate)


def test_generate_and_download_sow(monkeypatch) -> None:
    _stub_agent(monkeypatch)
    env = make_completed_envelope()
    runtime._envelopes[env.estimate_id] = env
    try:
        with TestClient(app) as c:
            r = c.post(f"/estimates/{env.estimate_id}/sow?scenario=ai_assisted")
            assert r.status_code == 200, r.text
            payload = r.json()
            assert payload["document"]["template_id"] == "default_sow"
            assert payload["document"]["sections"]
            assert "[CLIENT NAME]" in payload["document"]["placeholders"]

            # Round-trip the (unedited) document back to the docx renderer.
            r2 = c.post(f"/estimates/{env.estimate_id}/sow/docx", json={"document": payload["document"]})
            assert r2.status_code == 200, r2.text
            assert r2.headers["content-type"].startswith(_DOCX_MEDIA)
            assert "attachment" in r2.headers.get("content-disposition", "")
            assert r2.content[:2] == b"PK"
            assert len(r2.content) > 1000
    finally:
        runtime._envelopes.pop(env.estimate_id, None)


def test_generate_sow_rejects_incomplete_estimate(monkeypatch) -> None:
    _stub_agent(monkeypatch)
    env = EstimateEnvelope(
        estimate_id="pending-1",
        project_name="x",
        status=EstimateStatus.PASS_1_RUNNING,
        created_at=datetime.now(UTC),
    )
    runtime._envelopes[env.estimate_id] = env
    try:
        with TestClient(app) as c:
            r = c.post(f"/estimates/{env.estimate_id}/sow")
            assert r.status_code == 400
            assert "not completed" in r.json()["detail"].lower()
    finally:
        runtime._envelopes.pop(env.estimate_id, None)


def test_generate_sow_unknown_estimate_404(monkeypatch) -> None:
    _stub_agent(monkeypatch)
    with TestClient(app) as c:
        r = c.post("/estimates/does-not-exist-xyz/sow")
        assert r.status_code == 404

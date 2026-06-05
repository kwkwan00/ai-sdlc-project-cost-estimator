from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def test_health_endpoint_returns_ok() -> None:
    with _client() as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "service": "ai-sdlc-estimator"}


def test_get_unknown_estimate_returns_404() -> None:
    with _client() as c:
        r = c.get("/estimates/does-not-exist")
        assert r.status_code == 404


def test_submit_answers_for_unknown_estimate_returns_404() -> None:
    with _client() as c:
        r = c.post(
            "/estimates/does-not-exist/answers",
            json={"answers": {}, "skip_remaining": True},
        )
        assert r.status_code == 404

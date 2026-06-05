"""The backend lifespan must emit a ✓ ready log line so operators can grep for it."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient


def test_lifespan_emits_backend_ready_message(caplog) -> None:
    from main import app

    with caplog.at_level(logging.INFO, logger="main"):
        with TestClient(app) as client:
            # Hit /health to ensure startup completes synchronously.
            assert client.get("/health").status_code == 200

    ready_messages = [r.getMessage() for r in caplog.records if "Backend ready" in r.getMessage()]
    assert ready_messages, f"no 'Backend ready' log emitted. Records: {[r.getMessage() for r in caplog.records]}"
    # The message should include the bound host:port so operators see where to connect.
    assert "http://" in ready_messages[0]


def test_lifespan_emits_shutdown_message(caplog) -> None:
    from main import app

    with caplog.at_level(logging.INFO, logger="main"):
        with TestClient(app):
            pass  # context exit triggers lifespan shutdown

    shutdown_messages = [r.getMessage() for r in caplog.records if "shutting down" in r.getMessage()]
    assert shutdown_messages, "no shutdown log emitted"

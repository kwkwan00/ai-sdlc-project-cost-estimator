"""The backend lifespan must emit a ✓ ready log line so operators can grep for it.

Capture strategy: we attach our own handler directly to the ``main`` logger instead
of relying on pytest's ``caplog`` (whose handler lives on the *root* logger). The
lifespan runs ``alembic upgrade head`` when Postgres is enabled, and Alembic's
``fileConfig`` reconfigures the root logger from ``alembic.ini`` (root level → WARN,
root handlers → a single console handler). That wipes caplog's root handler and
raises the root level, so the subsequent INFO ``✓ Backend ready`` / shutdown lines —
emitted on the ``main`` logger — propagate to a root that no longer captures them.
Attaching to the ``main`` logger (which Alembic leaves untouched, and whose records
reach our handler before any propagation to root) makes the assertions hold whether
or not Postgres is reachable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient


@contextmanager
def _capture_main_logs() -> Iterator[list[logging.LogRecord]]:
    """Capture INFO+ records emitted on the ``main`` logger, robust to Alembic's
    ``fileConfig`` reconfiguring the *root* logger during lifespan startup."""
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    main_logger = logging.getLogger("main")
    handler = _Collector(level=logging.INFO)
    prev_level = main_logger.level
    main_logger.addHandler(handler)
    main_logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        main_logger.removeHandler(handler)
        main_logger.setLevel(prev_level)


def test_lifespan_emits_backend_ready_message() -> None:
    from main import app

    with _capture_main_logs() as records:
        with TestClient(app) as client:
            # Hit /health to ensure startup completes synchronously.
            assert client.get("/health").status_code == 200

    ready_messages = [r.getMessage() for r in records if "Backend ready" in r.getMessage()]
    assert ready_messages, f"no 'Backend ready' log emitted. Records: {[r.getMessage() for r in records]}"
    # The message should include the bound host:port so operators see where to connect.
    assert "http://" in ready_messages[0]


def test_lifespan_emits_shutdown_message() -> None:
    from main import app

    with _capture_main_logs() as records:
        with TestClient(app):
            pass  # context exit triggers lifespan shutdown

    shutdown_messages = [r.getMessage() for r in records if "shutting down" in r.getMessage()]
    assert shutdown_messages, "no shutdown log emitted"

"""Central logging configuration for the backend.

Call `configure_logging()` once at startup (from `main.py`) so every module's
``logging.getLogger(__name__)`` inherits a consistent format + level instead of
each module guessing. The level is driven by the ``LOG_LEVEL`` setting (env
``LOG_LEVEL``, default ``INFO``); a handful of chatty third-party loggers are
pinned to WARNING so the estimator's own logs stay legible.

This complements Langfuse tracing — Langfuse captures structured spans for LLM
calls; these logs are the plain-text operational narrative on stdout.
"""

from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Third-party loggers that flood INFO with per-request / driver-internal noise.
# Pinned to WARNING so the backend's own INFO lines remain readable.
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "anthropic",
    "neo4j",
    "urllib3",
    "openai",
    "alembic.runtime.plugins",
)

_configured = False


def configure_logging(level: str | None = None, *, force: bool = False) -> None:
    """Configure root logging once. Idempotent unless ``force=True``.

    `level` overrides the ``LOG_LEVEL`` setting (a name like ``"DEBUG"`` or a
    numeric level). Never raises — logging setup must not be able to crash the app.
    """
    global _configured
    if _configured and not force:
        return

    resolved = level
    if resolved is None:
        try:
            from config import get_settings

            resolved = get_settings().log_level
        except Exception:  # noqa: BLE001 - logging setup must never hard-fail
            resolved = "INFO"

    logging.basicConfig(level=resolved, format=_LOG_FORMAT, force=force)
    logging.getLogger().setLevel(resolved)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _configured = True

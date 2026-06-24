"""Context-local correlation id so every log line during an estimate carries its id.

A single estimate fans out across six twin nodes (and the support agents, MC layer,
persistence, …), all logging independently and concurrently. Without a correlation id
the lines from two in-flight estimates interleave indistinguishably. This binds the
``estimate_id`` into a ``contextvars.ContextVar`` at the start of each run; the async
tasks the run spawns inherit it, and ``EstimateIdFilter`` stamps it onto every
``LogRecord`` so the configured format can render ``[<estimate_id>]``.

Mirrors the ``orchestrator.usage`` accumulator pattern (also a per-run contextvar).
"""

from __future__ import annotations

import contextvars
import logging

# "-" is the unbound sentinel (rendered for any log line emitted outside an estimate run —
# startup, draft/prefill endpoints, health checks, tests).
_estimate_id: contextvars.ContextVar[str] = contextvars.ContextVar("estimate_id", default="-")


def bind_estimate_id(estimate_id: str) -> contextvars.Token[str]:
    """Bind ``estimate_id`` for the current context (and the async tasks it spawns).

    Returns the ``Token`` so a caller may ``reset_estimate_id`` it; binding inside a
    task-scoped coroutine (the background pass runners, the WBS request handlers) does
    not leak across runs because each gets its own context copy, so resetting is optional.
    """
    return _estimate_id.set(estimate_id or "-")


def reset_estimate_id(token: contextvars.Token[str]) -> None:
    """Restore the previous estimate id (best-effort; never raises)."""
    try:
        _estimate_id.reset(token)
    except (ValueError, LookupError):
        # Token from a different context (e.g. reset on a different task) — ignore.
        pass


def current_estimate_id() -> str:
    """The estimate id bound to the current context, or ``"-"`` when unbound."""
    return _estimate_id.get()


class EstimateIdFilter(logging.Filter):
    """Stamp the context-local estimate id onto every record as ``estimate_id``.

    Installed on the estimator's stdout handler so the format string's
    ``%(estimate_id)s`` always resolves (defaulting to ``"-"``) regardless of which
    module emitted the record. Returns True (a stamping filter, never a gate)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.estimate_id = _estimate_id.get()
        return True

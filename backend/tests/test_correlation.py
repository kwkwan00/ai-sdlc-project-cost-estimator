"""Estimate-id correlation: the contextvar bind + the logging filter that stamps it."""

from __future__ import annotations

import asyncio
import logging

from observability.correlation import (
    EstimateIdFilter,
    bind_estimate_id,
    current_estimate_id,
    reset_estimate_id,
)


def _record() -> logging.LogRecord:
    return logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)


def test_unbound_defaults_to_dash() -> None:
    assert current_estimate_id() == "-"
    rec = _record()
    assert EstimateIdFilter().filter(rec) is True  # stamping filter never gates
    assert rec.estimate_id == "-"


def test_bind_stamps_record_then_resets() -> None:
    token = bind_estimate_id("est-123")
    try:
        assert current_estimate_id() == "est-123"
        rec = _record()
        EstimateIdFilter().filter(rec)
        assert rec.estimate_id == "est-123"
    finally:
        reset_estimate_id(token)
    assert current_estimate_id() == "-"


def test_empty_id_falls_back_to_dash() -> None:
    token = bind_estimate_id("")
    try:
        assert current_estimate_id() == "-"
    finally:
        reset_estimate_id(token)


def test_id_propagates_to_spawned_task() -> None:
    # The whole point: a run binds the id, and the async tasks it spawns inherit it
    # (asyncio.create_task copies the current context), so the fan-out twins log it too.
    async def _child() -> str:
        return current_estimate_id()

    async def _main() -> str:
        bind_estimate_id("est-async")
        return await asyncio.create_task(_child())

    assert asyncio.run(_main()) == "est-async"
    # The bind happened inside asyncio.run's copied context — it must not leak back here.
    assert current_estimate_id() == "-"

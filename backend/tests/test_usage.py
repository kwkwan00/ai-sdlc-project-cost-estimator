"""Token-usage capture + cost estimation (orchestrator/usage.py)."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.usage import (
    bind_usage_accumulator,
    record_usage,
    summarize_usage,
)


def _entry(model: str, i: int, o: int, cr: int = 0) -> dict:
    return {"model": model, "input_tokens": i, "output_tokens": o, "cache_read_tokens": cr}


def test_summarize_totals_and_per_model_cost() -> None:
    acc = [
        _entry("claude-opus-4-8", 1000, 500),   # (1000*5 + 500*25)/1e6 = 0.0175
        _entry("claude-haiku-4-5", 2000, 1000),  # (2000*1 + 1000*5)/1e6 = 0.007
        _entry("claude-opus-4-8", 0, 0),         # a free/empty call still counts
    ]
    u = summarize_usage(acc)
    assert u.call_count == 3
    assert u.input_tokens == 3000
    assert u.output_tokens == 1500
    assert u.cost_usd == pytest.approx(0.0245)
    # Per-model breakdown, sorted by cost desc (opus first).
    assert [m.model for m in u.by_model] == ["claude-opus-4-8", "claude-haiku-4-5"]
    opus = u.by_model[0]
    assert opus.calls == 2 and opus.cost_usd == pytest.approx(0.0175)


def test_cache_read_billed_at_tenth_of_input() -> None:
    u = summarize_usage([_entry("claude-opus-4-8", 0, 0, cr=1000)])
    # 1000 * $5/1M * 0.1 = 0.0005
    assert u.cost_usd == pytest.approx(0.0005)
    assert u.cache_read_tokens == 1000


def test_unknown_model_falls_back_to_default_price() -> None:
    u = summarize_usage([_entry("some-future-model", 1_000_000, 0)])
    assert u.cost_usd == pytest.approx(5.0)  # default input price $5/1M


def test_record_usage_is_noop_without_bound_accumulator() -> None:
    # No accumulator bound in this context → silently does nothing (no raise).
    record_usage(model="claude-opus-4-8", input_tokens=1, output_tokens=1, cache_read_tokens=0)


def test_summarize_per_agent_breakdown_with_call_span() -> None:
    acc = [
        {"model": "claude-sonnet-4-6", "input_tokens": 100, "output_tokens": 50,
         "cache_read_tokens": 0, "agent": "submit_cocomo_assessment", "at": "2026-06-26T12:00:00+00:00"},
        {"model": "claude-sonnet-4-6", "input_tokens": 200, "output_tokens": 80,
         "cache_read_tokens": 0, "agent": "submit_cocomo_assessment", "at": "2026-06-26T12:00:05+00:00"},
        {"model": "claude-haiku-4-5", "input_tokens": 50, "output_tokens": 10,
         "cache_read_tokens": 0, "agent": "normalize_project_context", "at": "2026-06-26T11:59:00+00:00"},
    ]
    u = summarize_usage(acc)
    by_agent = {a.agent: a for a in u.by_agent}
    assert set(by_agent) == {"submit_cocomo_assessment", "normalize_project_context"}
    dev = by_agent["submit_cocomo_assessment"]
    assert dev.calls == 2
    assert dev.input_tokens == 300
    assert dev.model == "claude-sonnet-4-6"  # an agent calls one model
    # The agent's call span = min/max of its call timestamps.
    assert dev.started_at == "2026-06-26T12:00:00+00:00"
    assert dev.finished_at == "2026-06-26T12:00:05+00:00"


def test_usage_call_rows_one_row_per_call() -> None:
    from orchestrator.usage import usage_call_rows

    acc = [
        {"model": "claude-opus-4-8", "input_tokens": 1000, "output_tokens": 500,
         "cache_read_tokens": 0, "agent": "submit_cocomo_assessment", "at": "2026-06-26T12:00:00+00:00"},
        {"model": "claude-haiku-4-5", "input_tokens": 2000, "output_tokens": 1000,
         "cache_read_tokens": 0, "agent": "normalize_project_context", "at": "2026-06-26T11:00:00+00:00"},
    ]
    rows = usage_call_rows(acc)
    assert len(rows) == 2  # one row per recorded call
    assert rows[0]["agent"] == "submit_cocomo_assessment"
    assert rows[0]["model"] == "claude-opus-4-8"
    assert rows[0]["cost_usd"] == pytest.approx(0.0175)  # same pricing as summarize_usage
    assert rows[0]["called_at"] == "2026-06-26T12:00:00+00:00"


def test_record_usage_captures_agent_and_timestamp() -> None:
    import contextvars

    def _inner() -> list[dict]:
        # Bind inside a copied context so the binding doesn't leak to other tests.
        acc: list[dict] = []
        bind_usage_accumulator(acc)
        record_usage(
            model="claude-sonnet-4-6", input_tokens=1, output_tokens=1,
            cache_read_tokens=0, agent="propose_wbs",
        )
        return acc

    acc = contextvars.copy_context().run(_inner)
    assert len(acc) == 1
    assert acc[0]["agent"] == "propose_wbs"
    assert acc[0]["at"]  # a wall-clock timestamp was stamped
    assert summarize_usage(acc).by_agent[0].agent == "propose_wbs"


@pytest.mark.asyncio
async def test_capture_propagates_across_async_tasks() -> None:
    """The capture relies on contextvars reaching the graph's parallel node tasks —
    both directly-awaited coroutines and create_task children must hit the same list."""

    async def child(model: str) -> None:
        record_usage(model=model, input_tokens=10, output_tokens=5, cache_read_tokens=0)

    acc: list[dict] = []
    bind_usage_accumulator(acc)
    await asyncio.gather(
        child("claude-opus-4-8"),                  # same task
        asyncio.create_task(child("claude-haiku-4-5")),  # child task copies context
    )
    assert len(acc) == 2
    u = summarize_usage(acc)
    assert {m.model for m in u.by_model} == {"claude-opus-4-8", "claude-haiku-4-5"}


@pytest.mark.asyncio
async def test_capture_usage_to_db_restores_previous_accumulator() -> None:
    """A nested `capture_usage_to_db` must restore the accumulator that was bound before it, so the
    outer scope's subsequent `record_usage` calls aren't silently dropped into the inner (discarded)
    list. Postgres is off in tests, so the on-exit persist no-ops — this isolates the contextvar."""
    from orchestrator.usage import capture_usage_to_db

    outer: list[dict] = []
    bind_usage_accumulator(outer)

    async with capture_usage_to_db(session_id="wiz-restore"):
        # Recorded inside the block → goes to the inner accumulator, NOT `outer`.
        record_usage(model="claude-haiku-4-5", input_tokens=1, output_tokens=1, cache_read_tokens=0)
    assert outer == []  # nothing leaked into the outer list while the inner was bound

    # After the block, the outer accumulator is active again.
    record_usage(model="claude-opus-4-8", input_tokens=2, output_tokens=2, cache_read_tokens=0)
    assert len(outer) == 1
    assert outer[0]["model"] == "claude-opus-4-8"

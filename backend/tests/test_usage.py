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

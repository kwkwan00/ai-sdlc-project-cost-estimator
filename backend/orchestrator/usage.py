"""Per-run Anthropic token-usage capture + cost estimation.

`call_structured` records each LLM call's token usage into a context-local
accumulator. The HTTP layer binds a per-estimate list around the graph run (Pass 1
and Pass 2 append to the same list, tied together by estimate id), then summarizes
it into an `LlmUsage` for the final estimate — the meta-cost of producing it.

Capture is best-effort: when no list is bound (tests, stub path, pre-submission
agents) `record_usage` is a no-op, so nothing depends on it being active.
"""

from __future__ import annotations

import contextvars

from models.twin_outputs import LlmModelUsage, LlmUsage

# USD per 1M tokens, (input, output), matched as a substring against the model id.
# Cache-read tokens are billed at ~0.1x the input rate (Anthropic prompt caching).
_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "fable": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
_DEFAULT_PRICE = (5.0, 25.0)
_CACHE_READ_FRACTION = 0.1

_usage_acc: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "llm_usage_acc", default=None
)


def bind_usage_accumulator(acc: list[dict]) -> None:
    """Bind `acc` as the active accumulator for the current context (and the async
    tasks it spawns). Subsequent `record_usage` calls append to it."""
    _usage_acc.set(acc)


def record_usage(
    *, model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int
) -> None:
    """Append one call's token usage to the active accumulator, if any."""
    acc = _usage_acc.get()
    if acc is None:
        return
    acc.append(
        {
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read_tokens or 0),
        }
    )


def _price_for(model: str) -> tuple[float, float]:
    lowered = model.lower()
    for tag, price in _PRICING_PER_MTOK.items():
        if tag in lowered:
            return price
    return _DEFAULT_PRICE


def _call_cost(entry: dict) -> float:
    in_price, out_price = _price_for(entry["model"])
    return (
        entry["input_tokens"] * in_price
        + entry["output_tokens"] * out_price
        + entry["cache_read_tokens"] * in_price * _CACHE_READ_FRACTION
    ) / 1_000_000


def summarize_usage(acc: list[dict]) -> LlmUsage:
    """Fold a list of recorded calls into an `LlmUsage` with a per-model breakdown."""
    by_model: dict[str, dict] = {}
    total_cost = 0.0
    in_t = out_t = cr_t = 0
    for e in acc:
        cost = _call_cost(e)
        total_cost += cost
        in_t += e["input_tokens"]
        out_t += e["output_tokens"]
        cr_t += e["cache_read_tokens"]
        m = by_model.setdefault(
            e["model"],
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0},
        )
        m["calls"] += 1
        m["input_tokens"] += e["input_tokens"]
        m["output_tokens"] += e["output_tokens"]
        m["cache_read_tokens"] += e["cache_read_tokens"]
        m["cost_usd"] += cost
    return LlmUsage(
        call_count=len(acc),
        input_tokens=in_t,
        output_tokens=out_t,
        cache_read_tokens=cr_t,
        cost_usd=round(total_cost, 4),
        by_model=[
            LlmModelUsage(model=model, **{**v, "cost_usd": round(v["cost_usd"], 4)})
            for model, v in sorted(by_model.items(), key=lambda kv: -kv[1]["cost_usd"])
        ],
    )

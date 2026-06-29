"""Per-run Anthropic token-usage capture + cost estimation.

`call_structured` records each LLM call's token usage into a context-local
accumulator. The HTTP layer binds a per-estimate list around the graph run (Pass 1
and Pass 2 append to the same list, tied together by estimate id), then summarizes
it into an `LlmUsage` for the final estimate — the meta-cost of producing it.

Capture is best-effort: when no list is bound (tests, stub path) `record_usage` is a no-op, so
nothing depends on it being active. Agents that run **outside** the graph — the pre-submission
prefill / roster / tooling endpoints, the WBS planner, SOW generation — bind their own accumulator
(`capture_usage_to_db` for the pre-submission ones, which persists straight to `llm_call` with no
estimate id), so all LLM usage is captured, not just the graph's.
"""

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from models.twin_outputs import LlmAgentUsage, LlmModelUsage, LlmUsage

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


def bind_usage_accumulator(acc: list[dict]) -> contextvars.Token:
    """Bind `acc` as the active accumulator for the current context (and the async
    tasks it spawns). Subsequent `record_usage` calls append to it. Returns the
    contextvar `Token` so a scoped binder can restore the previous accumulator via
    `_usage_acc.reset(token)` (callers that don't nest can ignore the return)."""
    return _usage_acc.set(acc)


def record_usage(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    agent: str = "",
) -> None:
    """Append one call's token usage (+ the agent that made it, stamped with the wall-clock time) to
    the active accumulator, if any. `agent` is the call's forced-tool name (its agent identifier)."""
    acc = _usage_acc.get()
    if acc is None:
        return
    acc.append(
        {
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read_tokens or 0),
            "agent": agent or "unknown",
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )


@asynccontextmanager
async def capture_usage_to_db(*, estimate_id: str | None = None, session_id: str | None = None):
    """Bind a usage accumulator for a block of LLM work, then persist the captured per-call rows to
    the `llm_call` table on exit (best-effort). For agents that run **outside** the graph and its
    per-estimate accumulator — the pre-submission prefill / roster / tooling endpoints, which run
    before an estimate exists (`estimate_id=None`) — so their usage isn't discarded. The wizard-run
    `session_id` is stamped on the rows so they can later be associated with the estimate the wizard
    produces (`associate_llm_calls`).

    Yields the bound accumulator so a caller that also needs to **return** the cost (the WBS assist
    endpoints — reconcile / completeness / suggest-hours) can `summarize_usage(acc)` for the response
    without re-implementing the bind+persist dance; callers that only persist can ignore it."""
    acc: list[dict] = []
    token = bind_usage_accumulator(acc)
    try:
        yield acc
    finally:
        from db.repositories import insert_llm_calls

        try:
            await insert_llm_calls(
                usage_call_rows(acc), estimate_id=estimate_id, session_id=session_id
            )
        finally:
            # Restore whatever accumulator was bound before this block, so a nested capture
            # never leaves the contextvar pointing at this (now-discarded) list.
            _usage_acc.reset(token)


def usage_call_rows(acc: list[dict]) -> list[dict]:
    """Convert the raw accumulator into per-call rows for the `llm_call` table — one dict per recorded
    call, with the computed `cost_usd` and its `called_at` timestamp (ISO string). This is the
    per-call grain that powers DB-side observability aggregation."""
    return [
        {
            "agent": e.get("agent") or "unknown",
            "model": e["model"],
            "input_tokens": int(e["input_tokens"]),
            "output_tokens": int(e["output_tokens"]),
            "cache_read_tokens": int(e["cache_read_tokens"]),
            "cost_usd": round(_call_cost(e), 6),
            "called_at": e.get("at"),
        }
        for e in acc
    ]


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
    """Fold a list of recorded calls into an `LlmUsage` with a per-model AND per-agent breakdown (the
    per-agent rows carry the time span of that agent's calls)."""
    by_model: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
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
        a = by_agent.setdefault(
            e.get("agent") or "unknown",
            {"model": "", "calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
             "cost_usd": 0.0, "started_at": None, "finished_at": None},
        )
        a["model"] = a["model"] or e["model"]  # an agent calls one model — keep the first seen
        a["calls"] += 1
        a["input_tokens"] += e["input_tokens"]
        a["output_tokens"] += e["output_tokens"]
        a["cache_read_tokens"] += e["cache_read_tokens"]
        a["cost_usd"] += cost
        at = e.get("at")
        if at:  # ISO-8601 UTC strings sort lexicographically → min/max are the call span
            if a["started_at"] is None or at < a["started_at"]:
                a["started_at"] = at
            if a["finished_at"] is None or at > a["finished_at"]:
                a["finished_at"] = at
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
        by_agent=[
            LlmAgentUsage(agent=agent, **{**v, "cost_usd": round(v["cost_usd"], 4)})
            for agent, v in sorted(by_agent.items(), key=lambda kv: -kv[1]["cost_usd"])
        ],
    )

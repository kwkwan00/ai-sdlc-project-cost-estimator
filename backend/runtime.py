"""In-process runtime for the orchestrator backend: the SSE event broker, the
bounded in-memory registries, background-task tracking, run orchestration (Pass 1 /
Pass 2), and the best-effort persistence fan-out.

This module owns everything the HTTP routers need to *drive* an estimate but that is
not itself an HTTP endpoint. `main.py` is the thin app factory + lifespan that wires
the routers and feeds the compiled graph in here via `set_graph`. Splitting these out
keeps `main.py` to the app/lifespan surface while the routers import the registries
and helpers from one place.

Durability note (unchanged from the monolith): the registries and broker are
in-process only. The graph state lives in the LangGraph checkpointer; these dicts
back the API surface for the current process and are capped with oldest-first
eviction. `GET /estimates/{id}` (falling back to Postgres history) is the
authoritative source of current state, not the SSE stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from typing import Any

from db.neo4j_adapter import save_estimate_envelope
from db.repositories import refresh_calibration_for_phase, save_estimate_history
from models.project_schema import (
    EstimateEnvelope,
    EstimateStatus,
    Stage2Context,
    Stage3Context,
)
from models.twin_outputs import Phase

logger = logging.getLogger(__name__)


# Maximum number of estimates retained in the in-memory registries. The graph
# state lives in the LangGraph checkpointer; these dicts only back the API surface
# for the current process, so capping them with oldest-first eviction keeps memory
# bounded across many runs without affecting durability (completed estimates remain
# fetchable from Postgres via GET /estimates/{id}). MVP-simple LRU-ish policy.
_MAX_RETAINED_ESTIMATES = 256


class _EventBroker:
    """Per-estimate SSE fan-out with a bounded replay buffer.

    Durability guarantee: best-effort, in-process only. Every published event is
    appended to a bounded history list and pushed to all currently-subscribed
    queues. A new subscriber first replays the buffered history, then receives live
    events — so late joiners, reconnecting clients, and multiple concurrent
    subscribers all see the full backlog (no event stealing). Events are NOT durable
    across a process restart; `GET /estimates/{id}` is the authoritative source of
    truth for an estimate's current state. Event shapes are unchanged
    (`status` / `questions` / `final` / `error`).
    """

    # Cap history so a long-lived estimate can't grow the buffer without bound. A
    # run emits only a handful of events, so this is generous.
    _MAX_HISTORY = 128
    # Per-subscriber backpressure bound. A run emits ~6 events, so a subscriber this
    # far behind is stalled/dead; we drop it rather than let its queue grow unbounded.
    _MAX_QUEUE = 256

    def __init__(self) -> None:
        self.history: list[dict[str, str]] = []
        self.subscribers: set[asyncio.Queue] = set()

    async def publish(self, event: dict[str, str]) -> None:
        self.history.append(event)
        if len(self.history) > self._MAX_HISTORY:
            del self.history[: len(self.history) - self._MAX_HISTORY]
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow/stalled consumer: drop it instead of blocking the publisher (and
                # every other subscriber). It can reconnect and replay from history.
                self.unsubscribe(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._MAX_QUEUE)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


# In-memory registry of running estimates (state lives in the LangGraph checkpointer;
# this only tracks the envelope + last-known status for the API surface). Ordered so
# the oldest entry can be evicted first when over capacity.
_envelopes: OrderedDict[str, EstimateEnvelope] = OrderedDict()
_event_streams: dict[str, _EventBroker] = {}
# Per-estimate Anthropic token-usage accumulator. Pass 1 and Pass 2 append to the
# same list (tied by estimate id); summarized onto the final estimate after Pass 2.
# Always cleaned up in the run's `finally` block (both success and failure paths).
_llm_usage: dict[str, list[dict]] = {}

# Retained handles for fire-and-forget background tasks. Without this, the event
# loop only holds a weak reference and the task can be garbage-collected mid-run;
# the done-callback also surfaces otherwise-swallowed exceptions to the logger.
_background_tasks: set[asyncio.Task] = set()

# The compiled LangGraph graph, installed by the FastAPI lifespan via set_graph().
_graph: Any = None


def set_graph(graph: Any) -> None:
    """Install the compiled graph (called once from the lifespan on startup)."""
    global _graph
    _graph = graph


def _is_in_flight(env: EstimateEnvelope) -> bool:
    """An estimate is in-flight while a background task may still touch its state."""
    return env.status in (
        EstimateStatus.PENDING,
        EstimateStatus.PASS_1_RUNNING,
        EstimateStatus.AWAITING_ANSWERS,
        EstimateStatus.PASS_2_RUNNING,
        EstimateStatus.SYNTHESIZING,
    )


def remove_estimate(estimate_id: str) -> None:
    """Drop an estimate from the in-memory registries (envelope, event broker, usage
    accumulator). Idempotent — unknown ids are ignored. Persisted Postgres history is
    removed separately via the repository layer."""
    _envelopes.pop(estimate_id, None)
    _event_streams.pop(estimate_id, None)
    _llm_usage.pop(estimate_id, None)


def _evict_if_over_capacity() -> None:
    """Drop the oldest non-in-flight estimates once the registry is over capacity.

    Keeps the in-memory dicts bounded. Never evicts an estimate that's still running
    or awaiting answers; completed/failed estimates remain fetchable from Postgres.
    """
    while len(_envelopes) > _MAX_RETAINED_ESTIMATES:
        evicted = False
        for estimate_id, env in _envelopes.items():
            if _is_in_flight(env):
                continue
            _envelopes.pop(estimate_id, None)
            _event_streams.pop(estimate_id, None)
            _llm_usage.pop(estimate_id, None)
            evicted = True
            break
        if not evicted:
            # Everything retained is still in-flight — nothing safe to evict.
            break


def _attach_llm_usage(estimate_id: str) -> None:
    """Summarize the captured token usage onto the envelope's final estimate.

    Reads (does not pop) the accumulator; final cleanup happens in the run's
    `finally` block so usage is freed on both success and failure paths.
    """
    from orchestrator.usage import summarize_usage

    acc = _llm_usage.get(estimate_id)
    env = _envelopes.get(estimate_id)
    if acc and env and env.final_estimate is not None:
        env.final_estimate.llm_usage = summarize_usage(acc)


def _spawn_background(coro: Any, *, label: str) -> asyncio.Task:
    """Schedule a fire-and-forget coroutine, retaining a strong reference and
    logging any exception that escapes it."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("background task %s failed: %s", label, exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


def _config_for(estimate_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": estimate_id}}


async def _emit(estimate_id: str, event_type: str, data: dict[str, Any]) -> None:
    broker = _event_streams.get(estimate_id)
    if broker is not None:
        await broker.publish({"event": event_type, "data": json.dumps(data, default=str)})


def _refresh_envelope_from_state(estimate_id: str, state: dict[str, Any]) -> EstimateEnvelope:
    env = _envelopes[estimate_id]
    env.pass1_estimates = state.get("pass1_estimates", []) or env.pass1_estimates
    env.pass2_estimates = state.get("pass2_estimates", []) or env.pass2_estimates
    env.clarifying_questions = state.get("clarifying_questions", []) or env.clarifying_questions
    env.final_estimate = state.get("final_estimate") or env.final_estimate
    return env


async def _run_pass1(estimate_id: str, initial_state: dict[str, Any]) -> None:
    """Run the graph until it interrupts at Stage 4."""
    env = _envelopes[estimate_id]
    try:
        from orchestrator.usage import bind_usage_accumulator

        bind_usage_accumulator(_llm_usage.setdefault(estimate_id, []))
        env.status = EstimateStatus.PASS_1_RUNNING
        await _emit(estimate_id, "status", {"status": env.status.value})
        logger.info("estimate %s: pass 1 running", estimate_id)

        result = await _graph.ainvoke(initial_state, config=_config_for(estimate_id))
        _refresh_envelope_from_state(estimate_id, result)

        if result.get("__interrupt__"):
            env.status = EstimateStatus.AWAITING_ANSWERS
            await _emit(estimate_id, "status", {"status": env.status.value})
            await _emit(
                estimate_id,
                "questions",
                {"questions": [q.model_dump() for q in env.clarifying_questions]},
            )
            logger.info(
                "estimate %s: pass 1 paused, awaiting %d clarifying answer(s)",
                estimate_id,
                len(env.clarifying_questions),
            )
        else:
            # No interrupt — graph completed straight through.
            env.final_estimate = result.get("final_estimate")
            _attach_llm_usage(estimate_id)
            env.status = EstimateStatus.COMPLETED
            await _emit(estimate_id, "status", {"status": env.status.value})
            logger.info("estimate %s: completed (no clarifying interrupt)", estimate_id)
    except Exception as exc:  # noqa: BLE001
        env.status = EstimateStatus.FAILED
        env.error = str(exc)
        logger.exception("Pass 1 failed")
        await _emit(estimate_id, "error", {"message": str(exc)})
    finally:
        await _persist(
            env,
            initial_state.get("raw_input", ""),
            stage2=initial_state.get("stage2"),
            stage3=initial_state.get("stage3"),
        )
        # Free the usage accumulator on both paths once the run is no longer active.
        # While AWAITING_ANSWERS, Pass 2 still appends to the same list — keep it.
        if env.status != EstimateStatus.AWAITING_ANSWERS:
            _llm_usage.pop(estimate_id, None)
        _evict_if_over_capacity()


async def _resume_pass2(estimate_id: str, answers: dict[str, str]) -> None:
    from langgraph.types import Command

    env = _envelopes[estimate_id]
    stage2: Stage2Context | None = None
    stage3: Stage3Context | None = None
    try:
        from orchestrator.usage import bind_usage_accumulator

        # Same list Pass 1 used, so total usage spans both passes.
        bind_usage_accumulator(_llm_usage.setdefault(estimate_id, []))
        env.status = EstimateStatus.PASS_2_RUNNING
        await _emit(estimate_id, "status", {"status": env.status.value})
        logger.info("estimate %s: pass 2 running (resumed with answers)", estimate_id)

        result = await _graph.ainvoke(
            Command(resume={"answers": answers}),
            config=_config_for(estimate_id),
        )
        # Carry Stage 2/3 through to the post-run persist so the history row
        # stays fully populated on the Pass 2 update (initial state put them on
        # the graph; they survive both passes untouched).
        stage2 = result.get("stage2")
        stage3 = result.get("stage3")
        _refresh_envelope_from_state(estimate_id, result)
        env.final_estimate = result.get("final_estimate")
        _attach_llm_usage(estimate_id)
        env.status = EstimateStatus.COMPLETED
        await _emit(estimate_id, "status", {"status": env.status.value})
        await _emit(
            estimate_id,
            "final",
            env.final_estimate.model_dump() if env.final_estimate else {},
        )
        logger.info("estimate %s: completed (pass 2 synthesized)", estimate_id)
    except Exception as exc:  # noqa: BLE001
        env.status = EstimateStatus.FAILED
        env.error = str(exc)
        logger.exception("Pass 2 failed")
        await _emit(estimate_id, "error", {"message": str(exc)})
    finally:
        await _persist(env, "", stage2=stage2, stage3=stage3)
        # Pass 2 is terminal (completed or failed) — free the usage accumulator on
        # both paths so it never leaks (previously popped only on success).
        _llm_usage.pop(estimate_id, None)
        _evict_if_over_capacity()


async def _persist(
    env: EstimateEnvelope,
    raw_input: str,
    *,
    stage2: Stage2Context | None,
    stage3: Stage3Context | None,
) -> None:
    """Persist the envelope to both Neo4j (graph snapshot) and Postgres (history).

    Both writes are best-effort — failures are logged inside the adapters and
    don't propagate so the HTTP layer never fails because of persistence.
    """
    # Neo4j — graph snapshot for the calibration/history features.
    save_estimate_envelope(
        {
            "estimate_id": env.estimate_id,
            "project_name": env.project_name,
            "status": env.status.value,
            "raw_input": raw_input,
            "phases": [
                {
                    "phase": p.phase.value,
                    "twin_name": p.twin_name,
                    "algorithm": p.algorithm,
                    "ai_mid": p.ai_assisted_hours.most_likely,
                    "manual_mid": p.manual_only_hours.most_likely,
                    "confidence": p.confidence,
                }
                for p in (env.pass2_estimates or env.pass1_estimates)
            ],
        }
    )

    # Postgres — denormalized history + refresh of twin calibration aggregates.
    try:
        await save_estimate_history(env, stage2=stage2, stage3=stage3)
        if env.status == EstimateStatus.COMPLETED:
            # Iterate the Phase enum so a future 7th twin is picked up automatically
            # instead of silently missing calibration refresh. The per-phase refreshes
            # write disjoint rows, so run them concurrently.
            await asyncio.gather(
                *(refresh_calibration_for_phase(phase.value) for phase in Phase)
            )
            logger.info(
                "estimate %s: history persisted + calibration refreshed", env.estimate_id
            )
        else:
            logger.debug(
                "estimate %s: history persisted (status=%s)",
                env.estimate_id,
                env.status.value,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres history write failed (%s); continuing", exc)

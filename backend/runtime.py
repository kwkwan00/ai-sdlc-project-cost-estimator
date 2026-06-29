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
from collections.abc import Awaitable, Callable
from typing import Any

from db.neo4j_adapter import save_estimate_envelope, save_wbs_tree
from db.repositories import (
    associate_llm_calls,
    calls_from_summary,
    refresh_calibration_for_phase,
    save_estimate_history,
    save_llm_calls,
)
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

# Wizard-run UUID per estimate (set at creation). Lets `persist_completed_estimate` associate the
# pre-submission agents' `llm_call` rows (which carry the same session id) with this estimate.
_wizard_sessions: dict[str, str] = {}


def register_wizard_session(estimate_id: str, session_id: str | None) -> None:
    """Record the wizard-run UUID for an estimate so its pre-submission LLM calls are linked on persist."""
    if session_id:
        _wizard_sessions[estimate_id] = session_id

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
    accumulator, wizard-session map). Idempotent — unknown ids are ignored. Persisted
    Postgres history is removed separately via the repository layer."""
    _envelopes.pop(estimate_id, None)
    _event_streams.pop(estimate_id, None)
    _llm_usage.pop(estimate_id, None)
    # Pop the wizard-session entry too: deleting (or evicting) an estimate that paused at
    # AWAITING_ANSWERS means the persist `finally` that normally pops it never runs again,
    # so without this the map grows unbounded over create-then-delete-before-answer cycles.
    _wizard_sessions.pop(estimate_id, None)


# --- Public API for the HTTP layer (routers) ---------------------------------------------
# Routers use these instead of reaching into the private registries / coroutines, so the
# in-memory state stays owned by runtime and the module boundary is explicit.


def get_envelope(estimate_id: str) -> EstimateEnvelope | None:
    """The in-memory envelope for an estimate, or None when it isn't retained here (the
    caller then falls back to persisted history)."""
    return _envelopes.get(estimate_id)


def has_envelope(estimate_id: str) -> bool:
    return estimate_id in _envelopes


def register_envelope(
    estimate_id: str,
    env: EstimateEnvelope,
    *,
    with_event_stream: bool = False,
    evict: bool = False,
) -> None:
    """Store a new estimate envelope in the in-memory registry. Optionally create its SSE
    event broker (the twin flow streams) and/or evict the oldest non-in-flight entry over
    capacity (the WBS commit path, which doesn't go through `_run_graph_phase`)."""
    _envelopes[estimate_id] = env
    if with_event_stream:
        _event_streams[estimate_id] = _EventBroker()
    if evict:
        _evict_if_over_capacity()


def get_event_stream(estimate_id: str) -> _EventBroker:
    """The SSE event broker for an estimate. Callers gate on `has_envelope` first."""
    return _event_streams[estimate_id]


def spawn_background(coro: Any, *, label: str) -> asyncio.Task:
    """Schedule a fire-and-forget coroutine with a retained reference + escaped-exception
    logging (never use a bare `asyncio.create_task`)."""
    return _spawn_background(coro, label=label)


def start_pass1(estimate_id: str, initial_state: dict[str, Any]) -> None:
    """Kick off Pass 1 for a freshly-created estimate in the background."""
    _spawn_background(_run_pass1(estimate_id, initial_state), label=f"pass1:{estimate_id}")


def resume_pass2(estimate_id: str, answers: dict[str, str]) -> None:
    """Resume the interrupted graph with the user's Stage-4 answers (runs Pass 2)."""
    _spawn_background(_resume_pass2(estimate_id, answers), label=f"pass2:{estimate_id}")


async def resolve_envelope(estimate_id: str) -> EstimateEnvelope | None:
    """The authoritative current envelope: the in-memory copy if retained, else the persisted
    Postgres ``envelope_json`` snapshot (so a completed estimate resolves after a restart /
    eviction / in a fresh session). Returns None when neither has it. This is the single
    in-memory→Postgres fallback the read/duplicate/export routes share."""
    env = _envelopes.get(estimate_id)
    if env is not None:
        return env
    from db.repositories import get_estimate_envelope

    data = await get_estimate_envelope(estimate_id)
    return EstimateEnvelope.model_validate(data) if data is not None else None


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
            remove_estimate(estimate_id)  # drops envelope + event broker + usage accumulator
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


async def _run_graph_phase(
    estimate_id: str,
    *,
    running_status: EstimateStatus,
    invoke_arg: Any,
    running_log: str,
    fail_log: str,
    handle_result: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]],
    persist: dict[str, Any],
    keep_usage_when: Callable[[EstimateStatus], bool],
) -> None:
    """Shared run envelope for both graph passes: bind the usage accumulator, set the
    running status + emit it, invoke the graph, hand the result to the pass-specific
    ``handle_result`` (which mutates the envelope + emits + may return persist overrides),
    and on any exception mark the run FAILED + emit the error. The ``finally`` always
    persists and evicts; the per-pass ``keep_usage_when`` predicate decides whether the
    token accumulator survives (Pass 1 keeps it across the AWAITING_ANSWERS pause).

    Only the running/fail log lines, the invoke argument, the result handling, the persist
    inputs, and the usage-retention rule differ between Pass 1 and Pass 2 — everything else
    was byte-identical and lives here.
    """
    from observability.correlation import bind_estimate_id
    from orchestrator.usage import bind_usage_accumulator, usage_call_rows

    env = _envelopes[estimate_id]
    try:
        bind_estimate_id(estimate_id)
        bind_usage_accumulator(_llm_usage.setdefault(estimate_id, []))
        env.status = running_status
        await _emit(estimate_id, "status", {"status": env.status.value})
        logger.info(running_log, estimate_id)

        result = await _graph.ainvoke(invoke_arg, config=_config_for(estimate_id))
        overrides = await handle_result(estimate_id, result)
        if overrides:
            persist.update(overrides)
    except Exception as exc:  # noqa: BLE001
        env.status = EstimateStatus.FAILED
        env.error = str(exc)
        logger.exception(fail_log)
        await _emit(estimate_id, "error", {"message": str(exc)})
    finally:
        await persist_completed_estimate(
            env,
            raw_input=persist["raw_input"],
            stage2=persist["stage2"],
            stage3=persist["stage3"],
            # The raw per-call records (twin flow) → one llm_call row per call.
            llm_calls=usage_call_rows(_llm_usage.get(estimate_id) or []),
        )
        if not keep_usage_when(env.status):
            _llm_usage.pop(estimate_id, None)
            _wizard_sessions.pop(estimate_id, None)
        _evict_if_over_capacity()


async def _handle_pass1_result(estimate_id: str, result: dict[str, Any]) -> None:
    """Pass 1 result handling: pause at the Stage-4 interrupt (AWAITING_ANSWERS) or, if the
    graph ran straight through, finalize as COMPLETED. No persist overrides (Stage 2/3 came
    from the initial state)."""
    env = _envelopes[estimate_id]
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


async def _run_pass1(estimate_id: str, initial_state: dict[str, Any]) -> None:
    """Run the graph until it interrupts at Stage 4."""
    await _run_graph_phase(
        estimate_id,
        running_status=EstimateStatus.PASS_1_RUNNING,
        invoke_arg=initial_state,
        running_log="estimate %s: pass 1 running",
        fail_log="Pass 1 failed",
        handle_result=_handle_pass1_result,
        persist={
            "raw_input": initial_state.get("raw_input", ""),
            "stage2": initial_state.get("stage2"),
            "stage3": initial_state.get("stage3"),
        },
        # Keep the accumulator across the AWAITING_ANSWERS pause — Pass 2 appends to it.
        keep_usage_when=lambda status: status == EstimateStatus.AWAITING_ANSWERS,
    )


async def _handle_pass2_result(estimate_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Pass 2 result handling: finalize as COMPLETED and emit the final estimate. Returns the
    Stage 2/3 persist overrides carried off the graph state so the history row stays fully
    populated on the Pass 2 update (they survive both passes untouched)."""
    env = _envelopes[estimate_id]
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
    return {"stage2": result.get("stage2"), "stage3": result.get("stage3")}


async def _resume_pass2(estimate_id: str, answers: dict[str, str]) -> None:
    from langgraph.types import Command

    await _run_graph_phase(
        estimate_id,
        running_status=EstimateStatus.PASS_2_RUNNING,
        invoke_arg=Command(resume={"answers": answers}),
        running_log="estimate %s: pass 2 running (resumed with answers)",
        fail_log="Pass 2 failed",
        handle_result=_handle_pass2_result,
        # Stage 2/3 default to None until the successful invoke supplies them (override).
        persist={"raw_input": "", "stage2": None, "stage3": None},
        # Pass 2 is terminal — always free the accumulator (success or failure).
        keep_usage_when=lambda status: False,
    )


def build_estimate_snapshot(
    env: EstimateEnvelope, *, raw_input: str, phases: list
) -> dict[str, Any]:
    """The denormalized dict ``neo4j_adapter.save_estimate_envelope`` consumes (one Estimate node +
    its Phase nodes). Shared by the twin persistence path (``_persist``) and the WBS commit so the
    snapshot shape stays in one place."""
    return {
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
            for p in phases
        ],
    }


async def persist_completed_estimate(
    env: EstimateEnvelope,
    *,
    raw_input: str = "",
    stage2: Stage2Context | None,
    stage3: Stage3Context | None,
    wbs_tree: list | None = None,
    llm_calls: list[dict] | None = None,
    session_id: str | None = None,
) -> None:
    """Persist an estimate to both stores + refresh calibration — the single fan-out shared by the
    twin flow and the WBS commit (so neither re-implements the persist/calibration contract).

    * **Neo4j**: the denormalized Estimate+Phase snapshot, plus (WBS only) the task subgraph hung
      off the Estimate node (``wbs_tree`` → ``save_wbs_tree``; the envelope MERGE must precede it).
    * **Postgres**: denormalized history; and on ``COMPLETED`` a refresh of the per-phase
      calibration aggregates (so WBS estimates feed calibration exactly like twins).

    All best-effort — failures are logged and don't propagate, so the HTTP layer never fails on
    persistence. The two stores are written concurrently (they're independent)."""
    # Twin estimates carry their phases on pass2/pass1; WBS estimates carry them on final_estimate.
    phases = (
        env.pass2_estimates
        or env.pass1_estimates
        or (env.final_estimate.phases if env.final_estimate else [])
    )

    async def _graph() -> None:
        await save_estimate_envelope(build_estimate_snapshot(env, raw_input=raw_input, phases=phases))
        if wbs_tree is not None:
            from models.wbs_task import flatten_tree

            await save_wbs_tree(env.estimate_id, flatten_tree(wbs_tree, env.estimate_id))

    async def _history() -> None:
        try:
            await save_estimate_history(env, stage2=stage2, stage3=stage3)
            # Per-call LLM-usage rows (queryable + DB-aggregatable). Twins pass the raw per-call list;
            # the WBS commit passes None, so we derive per-agent rows from the carried-through summary.
            # Runs after the history upsert so the estimate row exists for the FK.
            rows = llm_calls
            if rows is None and env.final_estimate and env.final_estimate.llm_usage:
                rows = calls_from_summary(env.final_estimate.llm_usage.model_dump())
            await save_llm_calls(env.estimate_id, rows or [])
            # Associate the wizard's pre-submission calls (prefill/roster/tooling), which carry the
            # wizard session id, with this estimate now that its history row exists (FK). Idempotent.
            await associate_llm_calls(
                session_id or _wizard_sessions.get(env.estimate_id), env.estimate_id
            )
            if env.status == EstimateStatus.COMPLETED:
                # Iterate the Phase enum so a future 7th twin is picked up automatically instead of
                # silently missing calibration refresh. The per-phase refreshes write disjoint rows.
                await asyncio.gather(*(refresh_calibration_for_phase(phase.value) for phase in Phase))
                logger.info("estimate %s: history persisted + calibration refreshed", env.estimate_id)
            else:
                logger.debug(
                    "estimate %s: history persisted (status=%s)", env.estimate_id, env.status.value
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Postgres history write failed (%s); continuing", exc)

    async def _vectors() -> None:
        # Additive vector-similarity index (Qdrant) — completed estimates only, alongside (never
        # instead of) the Neo4j/Postgres writes above. Best-effort: no-ops without embeddings/Qdrant.
        if env.status != EstimateStatus.COMPLETED:
            return
        try:
            from orchestrator.calibration_index import index_completed_estimate

            await index_completed_estimate(
                env, raw_input=raw_input, stage2=stage2, stage3=stage3, wbs_tree=wbs_tree
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qdrant index write failed (%s); continuing", exc)

    await asyncio.gather(_graph(), _history(), _vectors())

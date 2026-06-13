"""FastAPI entrypoint — the full HTTP surface for the orchestrator backend.

Responsibilities concentrated in this module (a future improvement is to split it
into per-concern routers; that refactor is intentionally out of scope here):

  * Lifespan: run Postgres migrations, compile the LangGraph graph, dispose drivers
    on shutdown, and emit the "✓ Backend ready ..." readiness log.
  * Orchestration: kick off Pass 1 as a tracked background task, pause at the
    Stage 4 human-in-the-loop interrupt, and resume Pass 2 on submitted answers.
  * SSE event broker: a per-estimate fan-out broker with a bounded replay buffer so
    reconnecting / late / multiple subscribers all receive the run events
    (`status` / `questions` / `final` / `error`). The SSE stream is best-effort;
    `GET /estimates/{id}` is the authoritative source of current state.
  * Persistence fan-out: best-effort writes to Neo4j (graph snapshot) and Postgres
    (history + calibration refresh) after every status transition.
  * In-memory runtime registries (`_envelopes`, `_event_streams`, `_llm_usage`) with
    bounded eviction so they don't grow unbounded across many runs.

Endpoints (9):
  POST   /estimates/draft/prefill            -- LLM prefill for the Stage 2 wizard
  POST   /estimates/draft/classify-tooling   -- classify Stage 3 AI-tooling free text
  POST   /estimates/draft/roster/agui        -- AG-UI team-roster proposal run
  GET    /admin/reduction-bands              -- read AI-reduction guardrail bands
  PUT    /admin/reduction-bands              -- update AI-reduction guardrail bands
  POST   /estimates                          -- start a new estimation (Pass 1)
  GET    /estimates/history                  -- recent persisted estimates
  GET    /estimates/{id}                     -- fetch current state (source of truth)
  POST   /estimates/{id}/answers             -- submit Stage 4 answers (resumes Pass 2)
  GET    /estimates/{id}/stream              -- SSE stream of run events (best-effort)
  GET    /health                             -- liveness probe
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.types import Command
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from config import get_settings
from db.migrate import upgrade_to_head
from db.neo4j_adapter import close_driver, save_estimate_envelope
from db.postgres_adapter import dispose_engine as dispose_pg_engine
from db.postgres_adapter import get_engine as get_pg_engine
from db.qdrant_adapter import close_client as close_qdrant
from db.repositories import refresh_calibration_for_phase, save_estimate_history
from models.project_schema import (
    AnswerSubmission,
    CreateEstimateRequest,
    EstimateEnvelope,
    EstimateStatus,
    Stage2Context,
    Stage3Context,
)
from models.twin_outputs import Phase
from observability.langfuse_wrapper import shutdown as langfuse_shutdown
from observability.logging_config import configure_logging
from observability.request_logging import RequestLoggingMiddleware
from orchestrator.graph import build_graph
from prefill import DraftPrefillRequest, Stage2Prefill, prefill_stage2_from_raw
from reduction_bands_admin import (
    ReductionBandsResponse,
    ReductionBandsUpdate,
    get_effective_bands,
    update_bands,
)
from roster_agui import roster_agui_endpoint
from tooling_classifier import (
    ClassifyToolingRequest,
    ToolingClassification,
    classify_ai_tooling,
)

configure_logging()
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


def _is_in_flight(env: EstimateEnvelope) -> bool:
    """An estimate is in-flight while a background task may still touch its state."""
    return env.status in (
        EstimateStatus.PENDING,
        EstimateStatus.PASS_1_RUNNING,
        EstimateStatus.AWAITING_ANSWERS,
        EstimateStatus.PASS_2_RUNNING,
        EstimateStatus.SYNTHESIZING,
    )


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


_graph: Any = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _graph
    settings = get_settings()

    # Bring Postgres up before the graph compiles so calibration is available on
    # the very first request. Both calls are tolerant of Postgres being absent.
    if settings.postgres_enabled:
        # Run migrations in a thread so the async event loop isn't blocked by
        # Alembic's sync API.
        await asyncio.to_thread(upgrade_to_head)
        get_pg_engine()

    _graph = build_graph()
    logger.info("Orchestrator graph compiled.")
    logger.info(
        "✓ Backend ready at http://%s:%s  (health: /health, docs: /docs)",
        settings.backend_host,
        settings.backend_port,
    )
    yield
    logger.info("Backend shutting down — closing drivers + flushing traces")
    close_driver()
    close_qdrant()
    await dispose_pg_engine()
    langfuse_shutdown()


app = FastAPI(title="AI SDLC Cost Estimator", version="0.1.0", lifespan=lifespan)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Added last → outermost in the stack, so it logs every HTTP request (including
# CORS preflight) with method, path, status, and latency. Streaming-safe.
app.add_middleware(RequestLoggingMiddleware)


# Retained handles for fire-and-forget background tasks. Without this, the event
# loop only holds a weak reference and the task can be garbage-collected mid-run;
# the done-callback also surfaces otherwise-swallowed exceptions to the logger.
_background_tasks: set[asyncio.Task] = set()


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
            # instead of silently missing calibration refresh.
            for phase in Phase:
                await refresh_calibration_for_phase(phase.value)
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


@app.post("/estimates/draft/prefill", response_model=Stage2Prefill)
async def prefill_stage2(req: DraftPrefillRequest) -> Stage2Prefill:
    """LLM-driven prefill for the Stage 2 wizard step.

    Runs Claude over the raw Stage 1 description and returns a Stage2Context
    the frontend can hand to the form as default values. Always returns a
    valid response — the underlying extractor falls back to a minimal context
    (defaults + low confidence) when the LLM call fails, so the endpoint never
    surfaces an API-key or network error to the user.
    """
    return await prefill_stage2_from_raw(req.raw_input)


@app.post("/estimates/draft/classify-tooling", response_model=ToolingClassification)
async def classify_tooling(req: ClassifyToolingRequest) -> ToolingClassification:
    """Classify the freeform Stage 3 AI-tooling description into per-phase levels.

    The frontend sends the user's free-text tooling description on Stage 3 submit;
    this maps it to the six `AiToolingLevel`s the twins consume. Always returns a
    valid result — a blank description or any LLM/MCP failure degrades to all-'none'
    (no AI acceleration), so the endpoint never surfaces an error to the user.
    Tools the model can't identify are researched via the self-hosted docs-mcp-server;
    if that's unavailable, those tools stay 'none'.
    """
    return await classify_ai_tooling(req.description)


@app.get("/admin/reduction-bands", response_model=ReductionBandsResponse)
async def read_reduction_bands() -> ReductionBandsResponse:
    """Current AI-reduction guardrail bands (code defaults merged with DB overrides),
    as editable percentages — backs the Settings screen."""
    return await get_effective_bands()


@app.put("/admin/reduction-bands", response_model=ReductionBandsResponse)
async def write_reduction_bands(req: ReductionBandsUpdate) -> ReductionBandsResponse:
    """Persist edited AI-reduction bands and return the new effective state. When
    Postgres is disabled the change is not saved (the response's `editable` is false)."""
    return await update_bands(req)


# AG-UI agent-run endpoint for the team-roster proposal (Option B). The frontend
# fires this from Stage 2 (after prefill) and applies the streamed STATE_SNAPSHOT
# roster to the form. Registered via add_api_route so the handler's RunAgentInput
# body + Request signature drives FastAPI parsing.
app.add_api_route("/estimates/draft/roster/agui", roster_agui_endpoint, methods=["POST"])


@app.post("/estimates", response_model=EstimateEnvelope)
async def create_estimate(req: CreateEstimateRequest) -> EstimateEnvelope:
    estimate_id = str(uuid.uuid4())
    env = EstimateEnvelope(
        estimate_id=estimate_id,
        project_name=req.project_name or "Untitled estimate",
        status=EstimateStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    _envelopes[estimate_id] = env
    _event_streams[estimate_id] = _EventBroker()

    initial_state: dict[str, Any] = {
        "estimate_id": estimate_id,
        "project_name": env.project_name,
        "raw_input": req.raw_input,
        "stage2": req.stage2,
        "stage3": req.stage3,
        "parsed_context": {},
    }
    logger.info(
        "estimate %s created (project=%r); starting pass 1 in background",
        estimate_id,
        env.project_name,
    )
    _spawn_background(_run_pass1(estimate_id, initial_state), label=f"pass1:{estimate_id}")
    return env


class EstimateHistoryItem(BaseModel):
    estimate_id: str
    project_name: str
    status: str
    industry: str | None = None
    project_type: str | None = None
    total_ai_assisted_hours: float | None = None
    total_manual_only_hours: float | None = None
    ai_hours_saved: float | None = None
    total_cost_ai_assisted_usd: float | None = None
    confidence: float | None = None
    created_at: str | None = None
    updated_at: str | None = None


@app.get("/estimates/history", response_model=list[EstimateHistoryItem])
async def estimate_history() -> list[dict[str, Any]]:
    """Recent persisted estimates (newest first) for the dashboard history list.
    Empty when Postgres is disabled — history isn't kept in that case."""
    from db.repositories import list_estimate_history

    return await list_estimate_history()


@app.get("/estimates/{estimate_id}", response_model=EstimateEnvelope)
async def get_estimate(estimate_id: str) -> EstimateEnvelope:
    env = _envelopes.get(estimate_id)
    if env is not None:
        return env
    # Fall back to persisted history so a completed estimate redisplays even after a
    # restart / in a fresh session (when Postgres is connected).
    from db.repositories import get_estimate_envelope

    data = await get_estimate_envelope(estimate_id)
    if data is not None:
        return EstimateEnvelope.model_validate(data)
    raise HTTPException(404, "Estimate not found")


@app.post("/estimates/{estimate_id}/answers", response_model=EstimateEnvelope)
async def submit_answers(estimate_id: str, body: AnswerSubmission) -> EstimateEnvelope:
    env = _envelopes.get(estimate_id)
    if env is None:
        raise HTTPException(404, "Estimate not found")
    if env.status != EstimateStatus.AWAITING_ANSWERS:
        raise HTTPException(409, f"Estimate is in status {env.status.value}, not awaiting answers")
    logger.info(
        "estimate %s: received %d answer(s), resuming pass 2",
        estimate_id,
        len(body.answers),
    )
    _spawn_background(_resume_pass2(estimate_id, body.answers), label=f"pass2:{estimate_id}")
    return env


@app.get("/estimates/{estimate_id}/stream")
async def stream_estimate(estimate_id: str):
    """SSE stream of run events for one estimate.

    Best-effort delivery via an in-process fan-out broker with a replay buffer:
    a (re)connecting subscriber first receives the current status, then the full
    buffered backlog (so it never misses `questions` / `final` / `error`), then
    live events. Multiple concurrent subscribers each get their own queue and a
    full copy — no event stealing. For authoritative current state use
    `GET /estimates/{estimate_id}`.
    """
    if estimate_id not in _envelopes:
        raise HTTPException(404, "Estimate not found")
    broker = _event_streams[estimate_id]
    # Subscribe and snapshot the backlog together with no `await` in between so a
    # concurrently-published event can't slip through the gap. Because asyncio is
    # single-threaded and publish() appends to history *and* fans out to every
    # subscriber queue in one synchronous step, the backlog holds exactly the events
    # published before this subscription and the queue holds exactly those after —
    # the two are disjoint, so no de-duplication is needed.
    queue = broker.subscribe()
    backlog = list(broker.history)

    async def gen():
        try:
            # Send current status immediately so reconnecting clients catch up.
            env = _envelopes[estimate_id]
            yield {"event": "status", "data": json.dumps({"status": env.status.value})}
            # Replay buffered backlog so a late/reconnecting client doesn't miss any
            # already-published events before switching to live delivery.
            for ev in backlog:
                yield ev
                if ev["event"] in ("final", "error"):
                    return
            # Live events published after this subscription started.
            while True:
                ev = await queue.get()
                yield ev
                if ev["event"] in ("final", "error"):
                    break
        finally:
            broker.unsubscribe(queue)

    return EventSourceResponse(gen())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-sdlc-estimator"}

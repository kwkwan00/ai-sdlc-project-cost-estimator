"""Core estimate lifecycle endpoints + liveness probe.

Create an estimate (kicks off Pass 1 in the background), fetch current state (the
authoritative source of truth, falling back to Postgres history), submit Stage 4
answers (resumes Pass 2), and the SSE run-event stream (best-effort).

The handlers own only the HTTP surface; the registries, broker, run orchestration,
and persistence fan-out live in `runtime.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import runtime
from models.project_schema import (
    AnswerSubmission,
    CreateEstimateRequest,
    EstimateEnvelope,
    EstimateStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["estimates"])


@router.post("/estimates", response_model=EstimateEnvelope)
async def create_estimate(req: CreateEstimateRequest) -> EstimateEnvelope:
    estimate_id = str(uuid.uuid4())
    env = EstimateEnvelope(
        estimate_id=estimate_id,
        project_name=req.project_name or "Untitled estimate",
        status=EstimateStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    runtime.register_envelope(estimate_id, env, with_event_stream=True)
    # Tie the wizard's pre-submission LLM calls (prefill/roster/tooling) to this estimate, so they're
    # associated with it in the llm_call table when it persists (Observability).
    runtime.register_wizard_session(estimate_id, req.session_id)

    initial_state: dict[str, Any] = {
        "estimate_id": estimate_id,
        "project_name": env.project_name,
        "raw_input": req.raw_input,
        "stage2": req.stage2,
        "stage3": req.stage3,
        "parsed_context": {},
    }
    # Omit the key entirely when None so the twin guard's "absent ⇒ all phases" back-compat path
    # runs (rather than seeding an empty list, which would skip every twin).
    if req.selected_phases:
        initial_state["selected_phases"] = req.selected_phases
    logger.info(
        "estimate %s created (project=%r); starting pass 1 in background",
        estimate_id,
        env.project_name,
    )
    runtime.start_pass1(estimate_id, initial_state)
    return env


class EstimateHistoryItem(BaseModel):
    estimate_id: str
    project_name: str
    status: str
    # "twins" (default top-down flow) or "wbs" (bottom-up). Lets the dashboard badge + offer
    # the WBS-only Duplicate action. Defaulted so pre-WBS history rows deserialize.
    method: str = "twins"
    industry: str | None = None
    project_type: str | None = None
    total_ai_assisted_hours: float | None = None
    total_manual_only_hours: float | None = None
    ai_hours_saved: float | None = None
    total_cost_ai_assisted_usd: float | None = None
    confidence: float | None = None
    created_at: str | None = None
    updated_at: str | None = None


class EstimateHistoryPage(BaseModel):
    """One page of history rows plus the full row count for the dashboard's page
    controls."""

    items: list[EstimateHistoryItem]
    total: int


@router.get("/estimates/history", response_model=EstimateHistoryPage)
async def estimate_history(
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Recent persisted estimates (newest first), paged for the dashboard. `total`
    is the full row count so the client can render page controls. Returns an empty
    page (total=0) when Postgres is disabled — history isn't kept in that case."""
    from db.repositories import count_estimate_history, list_estimate_history

    items, total = await asyncio.gather(
        list_estimate_history(limit=limit, offset=offset),
        count_estimate_history(),
    )
    return {"items": items, "total": total}


@router.get("/estimates/{estimate_id}", response_model=EstimateEnvelope)
async def get_estimate(estimate_id: str) -> EstimateEnvelope:
    # The authoritative state: in-memory if retained, else the persisted snapshot (so a
    # completed estimate redisplays after a restart / in a fresh session).
    env = await runtime.resolve_envelope(estimate_id)
    if env is None:
        raise HTTPException(404, "Estimate not found")
    return env


@router.delete("/estimates/{estimate_id}", status_code=204)
async def delete_estimate(estimate_id: str) -> Response:
    """Delete an estimate: drop it from the in-memory registries and remove its
    persisted history (+ phase rows), so it no longer appears on the dashboard or
    resolves via GET. Idempotent — returns 204 even if it was already gone."""
    from db.repositories import delete_estimate_history

    runtime.remove_estimate(estimate_id)
    await delete_estimate_history(estimate_id)
    return Response(status_code=204)


@router.post("/estimates/{estimate_id}/answers", response_model=EstimateEnvelope)
async def submit_answers(estimate_id: str, body: AnswerSubmission) -> EstimateEnvelope:
    env = runtime.get_envelope(estimate_id)
    if env is None:
        raise HTTPException(404, "Estimate not found")
    if env.status != EstimateStatus.AWAITING_ANSWERS:
        raise HTTPException(409, f"Estimate is in status {env.status.value}, not awaiting answers")
    logger.info(
        "estimate %s: received %d answer(s), resuming pass 2",
        estimate_id,
        len(body.answers),
    )
    runtime.resume_pass2(estimate_id, body.answers)
    return env


@router.get("/estimates/{estimate_id}/stream")
async def stream_estimate(estimate_id: str):
    """SSE stream of run events for one estimate.

    Best-effort delivery via an in-process fan-out broker with a replay buffer:
    a (re)connecting subscriber first receives the current status, then the full
    buffered backlog (so it never misses `questions` / `final` / `error`), then
    live events. Multiple concurrent subscribers each get their own queue and a
    full copy — no event stealing. For authoritative current state use
    `GET /estimates/{estimate_id}`.
    """
    if not runtime.has_envelope(estimate_id):
        raise HTTPException(404, "Estimate not found")
    broker = runtime.get_event_stream(estimate_id)
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
            # The estimate may have been evicted or DELETEd between subscription and
            # the generator starting — short-circuit cleanly instead of KeyError'ing
            # inside the stream (mirrors the get-with-fallback pattern above).
            env = runtime.get_envelope(estimate_id)
            if env is None:
                yield {"event": "error", "data": json.dumps({"error": "Estimate not found"})}
                return
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


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-sdlc-estimator"}

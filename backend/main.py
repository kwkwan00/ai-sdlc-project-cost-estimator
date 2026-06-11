"""FastAPI entrypoint. Exposes the orchestrator graph as REST + SSE.

Endpoints:
  POST   /estimates                      -- start a new estimation
  GET    /estimates/{id}                 -- fetch current state
  GET    /estimates/{id}/stream          -- SSE stream of run events
  POST   /estimates/{id}/answers         -- submit Stage 4 answers (resumes from interrupt)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.types import Command
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
    Stage3Maturity,
)
from observability.langfuse_wrapper import shutdown as langfuse_shutdown
from observability.logging_config import configure_logging
from orchestrator.graph import build_graph
from prefill import DraftPrefillRequest, Stage2Prefill, prefill_stage2_from_raw
from roster_agui import roster_agui_endpoint

configure_logging()
logger = logging.getLogger(__name__)


# In-memory registry of running estimates (state lives in the LangGraph checkpointer;
# this only tracks the thread_id + last-known status for the API surface).
_envelopes: dict[str, EstimateEnvelope] = {}
_event_streams: dict[str, asyncio.Queue] = {}
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


def _config_for(estimate_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": estimate_id}}


async def _emit(estimate_id: str, event_type: str, data: dict[str, Any]) -> None:
    queue = _event_streams.get(estimate_id)
    if queue is not None:
        await queue.put({"event": event_type, "data": json.dumps(data, default=str)})


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


async def _resume_pass2(estimate_id: str, answers: dict[str, str]) -> None:
    env = _envelopes[estimate_id]
    stage2: Stage2Context | None = None
    stage3: Stage3Maturity | None = None
    try:
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


async def _persist(
    env: EstimateEnvelope,
    raw_input: str,
    *,
    stage2: Stage2Context | None,
    stage3: Stage3Maturity | None,
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
            for phase_value in (
                "discovery",
                "ux_design",
                "development",
                "code_review",
                "deployment",
                "qa_testing",
            ):
                await refresh_calibration_for_phase(phase_value)
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
        created_at=datetime.utcnow(),
    )
    _envelopes[estimate_id] = env
    _event_streams[estimate_id] = asyncio.Queue(maxsize=64)

    initial_state = {
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
    asyncio.create_task(_run_pass1(estimate_id, initial_state))
    return env


@app.get("/estimates/{estimate_id}", response_model=EstimateEnvelope)
async def get_estimate(estimate_id: str) -> EstimateEnvelope:
    env = _envelopes.get(estimate_id)
    if env is None:
        raise HTTPException(404, "Estimate not found")
    return env


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
    asyncio.create_task(_resume_pass2(estimate_id, body.answers))
    return env


@app.get("/estimates/{estimate_id}/stream")
async def stream_estimate(estimate_id: str):
    if estimate_id not in _envelopes:
        raise HTTPException(404, "Estimate not found")
    queue = _event_streams[estimate_id]

    async def gen():
        # Send current status immediately so reconnecting clients catch up.
        env = _envelopes[estimate_id]
        yield {"event": "status", "data": json.dumps({"status": env.status.value})}
        while True:
            ev = await queue.get()
            yield ev
            if ev["event"] in ("final", "error"):
                break

    return EventSourceResponse(gen())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-sdlc-estimator"}

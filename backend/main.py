"""FastAPI entrypoint — the thin app factory + lifespan for the orchestrator backend.

The HTTP surface is split across focused routers (`routers/drafts.py`,
`routers/admin.py`, `routers/estimates.py`); the in-process runtime — SSE event
broker, bounded in-memory registries, background-task tracking, Pass 1 / Pass 2
orchestration, and the best-effort persistence fan-out — lives in `runtime.py`. This
module is left with only:

  * Lifespan: run Postgres migrations, compile the LangGraph graph (handing it to
    `runtime.set_graph`), dispose drivers on shutdown, and emit the
    "✓ Backend ready ..." readiness log.
  * App factory + middleware (CORS + request-logging) + router wiring.

Endpoints (9), grouped by router:
  routers/drafts.py    POST /estimates/draft/prefill            -- Stage 2 prefill
                       POST /estimates/draft/classify-tooling   -- Stage 3 tooling classify
                       POST /estimates/draft/roster/agui        -- AG-UI roster proposal
  routers/admin.py     GET  /admin/reduction-bands              -- read reduction bands
                       PUT  /admin/reduction-bands              -- update reduction bands
  routers/estimates.py POST /estimates                          -- start estimation (Pass 1)
                       GET  /estimates/history                  -- recent persisted estimates
                       GET  /estimates/{id}                     -- current state (source of truth)
                       POST /estimates/{id}/answers             -- submit Stage 4 answers
                       GET  /estimates/{id}/stream              -- SSE run events (best-effort)
                       GET  /health                             -- liveness probe
  routers/wbs.py       POST /wbs/draft                          -- LLM-draft a WBS tree (resumable)
                       GET  /wbs/drafts                         -- resume list
                       GET/PUT/DELETE /wbs/drafts/{id}          -- load / autosave / discard a draft
                       POST /wbs/drafts/{id}/duplicate          -- clone a draft
                       POST /estimates/{id}/wbs/duplicate       -- clone a completed WBS estimate
                       POST /estimates/wbs/preview              -- roll up without persisting
                       POST /estimates/wbs                      -- commit a WBS estimate
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from db.migrate import upgrade_to_head
from db.neo4j_adapter import close_driver
from db.postgres_adapter import dispose_engine as dispose_pg_engine
from db.postgres_adapter import get_engine as get_pg_engine
from db.qdrant_adapter import close_client as close_qdrant
from observability.langfuse_wrapper import shutdown as langfuse_shutdown
from observability.logging_config import configure_logging
from observability.request_logging import RequestLoggingMiddleware
from orchestrator.graph import build_graph
from routers import admin as admin_router
from routers import catalog as catalog_router
from routers import drafts as drafts_router
from routers import estimates as estimates_router
from routers import sow as sow_router
from routers import wbs as wbs_router
from runtime import set_graph

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()

    # Bring Postgres up before the graph compiles so calibration is available on
    # the very first request. Both calls are tolerant of Postgres being absent.
    if settings.postgres_enabled:
        # Run migrations in a thread so the async event loop isn't blocked by
        # Alembic's sync API.
        await asyncio.to_thread(upgrade_to_head)
        get_pg_engine()

    set_graph(build_graph())
    logger.info("Orchestrator graph compiled.")
    logger.info(
        "✓ Backend ready at http://%s:%s  (health: /health, docs: /docs)",
        settings.backend_host,
        settings.backend_port,
    )
    yield
    logger.info("Backend shutting down — closing drivers + flushing traces")
    await close_driver()
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

# Wire the per-concern routers. Order mirrors the original single-file registration;
# within routers/estimates.py the literal /estimates/history route is declared before
# /estimates/{estimate_id} so it isn't shadowed by the path param.
app.include_router(drafts_router.router)
app.include_router(admin_router.router)
app.include_router(catalog_router.router)
app.include_router(estimates_router.router)
# WBS bottom-up flow (separate from the twin orchestrator). Mounted after estimates so its
# literal /estimates/wbs* routes coexist with /estimates/{id} (distinct methods/paths).
app.include_router(wbs_router.router)
# SOW export: POST /estimates/{id}/sow + /sow/docx. Literal sub-paths under /estimates/{id};
# distinct methods so they coexist with the estimate routes.
app.include_router(sow_router.router)

"""WBS (Work Breakdown Structure) flow endpoints — the bottom-up estimation surface.

Separate from the twin orchestrator (`routers/estimates.py`): drafts are LLM-seeded, edited,
re-evaluated deterministically, and committed into a normal `EstimateEnvelope`. Drafts persist
graph-natively in Neo4j (resumable, duplicable); the committed estimate also lands in Postgres
history so it shows up on the dashboard alongside twin estimates.

The Neo4j adapter is async (it uses the driver's `AsyncGraphDatabase` API), so these handlers
`await` its calls directly; the commit path runs the independent best-effort writes concurrently
with `asyncio.gather`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response

import runtime
from agents.wbs_agent import generate_wbs_tree
from db.neo4j_adapter import (
    delete_wbs_draft,
    get_driver,
    list_wbs_drafts,
    load_wbs_draft,
    save_wbs_draft,
)
from models.project_schema import (
    EstimateEnvelope,
    EstimateStatus,
    RoleRoster,
    Stage2Context,
    Stage3Context,
)
from models.twin_outputs import DualScenarioEstimate
from models.wbs_schema import (
    WbsCalculateRequest,
    WbsDraft,
    WbsDraftList,
    WbsDraftRequest,
    WbsDraftResponse,
    WbsDraftSaveRequest,
    WbsDraftSummary,
)
from models.wbs_task import WbsTaskInput, flatten_tree, rebuild_tree, regenerate_ids
from orchestrator.wbs.rollup import build_wbs_estimate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["wbs"])


# --- storage <-> model mapping -------------------------------------------------------------


def _to_storage(
    draft_id: str,
    *,
    project_name: str,
    raw_input: str,
    tree: list[WbsTaskInput],
    stage2: Stage2Context | None,
    stage3: Stage3Context | None,
    contingency_pct: float | None = None,
) -> dict:
    """Flatten a draft into the Neo4j adapter's row shape (tree → flat task rows, context → JSON)."""
    return {
        "draft_id": draft_id,
        "project_name": project_name,
        "raw_input": raw_input,
        "stage2_json": stage2.model_dump_json() if stage2 else None,
        "stage3_json": stage3.model_dump_json() if stage3 else None,
        "contingency_pct": contingency_pct,
        "tasks": flatten_tree(tree, draft_id),
    }


def _from_storage(data: dict) -> WbsDraft:
    """Rebuild a `WbsDraft` from the adapter's loaded rows."""
    draft_id = data["draft_id"]
    tree = rebuild_tree(data.get("tasks", []), draft_id)
    s2_json = data.get("stage2_json")
    s3_json = data.get("stage3_json")
    return WbsDraft(
        draft_id=draft_id,
        project_name=data.get("project_name", "") or "",
        raw_input=data.get("raw_input", "") or "",
        tree=tree,
        stage2=Stage2Context.model_validate_json(s2_json) if s2_json else None,
        stage3=Stage3Context.model_validate_json(s3_json) if s3_json else None,
        contingency_pct=data.get("contingency_pct"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def _copy_name(name: str) -> str:
    """Name for a duplicated draft. Re-duplicating an "X (Copy)" stays "X (Copy)" rather than
    compounding into "X (Copy) (Copy)" on every clone."""
    if not name:
        return "WBS draft (Copy)"
    return name if name.endswith("(Copy)") else f"{name} (Copy)"


async def _duplicate_into_draft(
    *,
    tree: list[WbsTaskInput],
    stage2: Stage2Context | None,
    stage3: Stage3Context | None,
    name: str,
    raw_input: str,
    contingency_pct: float | None = None,
) -> WbsDraftResponse:
    """Clone a tree + context into a brand-new persisted draft (fresh ids, ' (Copy)' name)."""
    new_id = str(uuid.uuid4())
    new_tree = regenerate_ids(tree, lambda: uuid.uuid4().hex)
    await save_wbs_draft(
        _to_storage(
            new_id, project_name=_copy_name(name), raw_input=raw_input, tree=new_tree,
            stage2=stage2, stage3=stage3, contingency_pct=contingency_pct,
        ),
    )
    logger.info("WBS draft %s duplicated → %s", "<source>", new_id)
    return WbsDraftResponse(draft_id=new_id, tree=new_tree, notes="")


# --- draft lifecycle -----------------------------------------------------------------------


@router.post("/wbs/draft", response_model=WbsDraftResponse)
async def draft_wbs(req: WbsDraftRequest) -> WbsDraftResponse:
    """Generate an LLM-drafted WBS tree, persist it as a resumable draft, and return it.

    Always returns an editable tree — the planner degrades to a deterministic skeleton when the
    LLM is unavailable. The draft persists to Neo4j (best-effort; resume needs Neo4j up)."""
    tree, notes = await generate_wbs_tree(req)
    draft_id = str(uuid.uuid4())
    await save_wbs_draft(
        _to_storage(
            draft_id, project_name=req.project_name or "", raw_input=req.raw_input,
            tree=tree, stage2=req.stage2, stage3=req.stage3,
        ),
    )
    return WbsDraftResponse(draft_id=draft_id, tree=tree, notes=notes)


@router.get("/wbs/drafts", response_model=WbsDraftList)
async def list_drafts() -> WbsDraftList:
    """The 'resume a draft' list (newest first). `resumable=false` signals Neo4j is off."""
    rows = await list_wbs_drafts(50)
    resumable = await get_driver() is not None
    return WbsDraftList(
        items=[WbsDraftSummary(**r) for r in rows], resumable=resumable
    )


@router.get("/wbs/drafts/{draft_id}", response_model=WbsDraft)
async def get_draft(draft_id: str) -> WbsDraft:
    """Load a draft to resume editing. 404 when absent / Neo4j off (client falls back to cache)."""
    data = await load_wbs_draft(draft_id)
    if data is None:
        raise HTTPException(404, "WBS draft not found")
    return _from_storage(data)


@router.put("/wbs/drafts/{draft_id}", response_model=WbsDraft)
async def save_draft(draft_id: str, body: WbsDraftSaveRequest) -> WbsDraft:
    """Autosave the editor state for a draft (idempotent rebuild-on-save)."""
    await save_wbs_draft(
        _to_storage(
            draft_id, project_name=body.project_name, raw_input=body.raw_input,
            tree=body.tree, stage2=body.stage2, stage3=body.stage3,
            contingency_pct=body.contingency_pct,
        ),
    )
    return WbsDraft(
        draft_id=draft_id,
        project_name=body.project_name,
        raw_input=body.raw_input,
        tree=body.tree,
        stage2=body.stage2,
        stage3=body.stage3,
        contingency_pct=body.contingency_pct,
    )


@router.delete("/wbs/drafts/{draft_id}", status_code=204)
async def delete_draft(draft_id: str) -> Response:
    """Discard a draft + its task subgraph. Idempotent — 204 even if it was already gone."""
    await delete_wbs_draft(draft_id)
    return Response(status_code=204)


# --- duplicate -----------------------------------------------------------------------------


@router.post("/wbs/drafts/{draft_id}/duplicate", response_model=WbsDraftResponse)
async def duplicate_draft(draft_id: str) -> WbsDraftResponse:
    """Clone an in-progress draft into a new editable draft."""
    data = await load_wbs_draft(draft_id)
    if data is None:
        raise HTTPException(404, "WBS draft not found")
    draft = _from_storage(data)
    return await _duplicate_into_draft(
        tree=draft.tree, stage2=draft.stage2, stage3=draft.stage3,
        name=draft.project_name, raw_input=draft.raw_input,
        contingency_pct=draft.contingency_pct,
    )


@router.post("/estimates/{estimate_id}/wbs/duplicate", response_model=WbsDraftResponse)
async def duplicate_from_estimate(estimate_id: str) -> WbsDraftResponse:
    """Clone a completed WBS estimate (from its review) into a new editable draft.

    Sources the tree + context from the envelope (in-memory or persisted `envelope_json`), so it
    works even with Neo4j off. 409 if the estimate isn't a WBS estimate."""
    env = await runtime.resolve_envelope(estimate_id)
    if env is None:
        raise HTTPException(404, "Estimate not found")
    if env.method != "wbs" or not env.wbs_tree:
        raise HTTPException(409, "Estimate is not a WBS estimate")
    # The applied contingency rides on the final estimate; carry it so the clone matches the source.
    contingency = env.final_estimate.contingency_pct if env.final_estimate else None
    return await _duplicate_into_draft(
        tree=env.wbs_tree, stage2=env.wbs_stage2, stage3=env.wbs_stage3,
        name=env.project_name, raw_input="", contingency_pct=contingency,
    )


# --- compute -------------------------------------------------------------------------------


def _stable_seed(req: WbsCalculateRequest) -> str:
    """A deterministic Monte-Carlo seed for a tree, shared by preview AND commit.

    The MC bands/percentiles are seeded from the ``estimate_id`` passed into
    ``build_wbs_estimate``. If preview used one ephemeral id and commit a different fresh
    uuid, the numbers the user RE-EVALUATED (preview) would not match what got SAVED
    (commit) for the same tree — surprising and incorrect. So both endpoints seed off this
    stable value instead: the ``draft_id`` when present (stable across a draft's lifetime),
    else a content hash of the rolled-up tree (the only inputs the rollup's RNG consumes are
    the per-leaf 3-point bands + phase/role grouping, so hashing the flattened tree is
    sufficient and order-stable). The committed envelope keeps its own unique
    ``estimate_id`` for identity — only the RNG seed is made deterministic here."""
    if req.draft_id:
        return f"wbs-seed:draft:{req.draft_id}"
    rows = flatten_tree(req.tree, "seed")
    payload = json.dumps(rows, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
    return f"wbs-seed:tree:{digest}"


@router.post("/estimates/wbs/preview", response_model=DualScenarioEstimate)
async def preview_wbs(req: WbsCalculateRequest) -> DualScenarioEstimate:
    """Roll the current tree up into an estimate WITHOUT persisting (the editor's Re-evaluate).

    Seeds the MC off ``_stable_seed(req)`` so re-evaluating then committing the SAME tree
    yields identical numbers (see ``_stable_seed``)."""
    from observability.correlation import bind_estimate_id

    seed = _stable_seed(req)
    bind_estimate_id(f"wbs-preview:{seed}")
    return await build_wbs_estimate(req, estimate_id=seed)


@router.post("/estimates/wbs", response_model=EstimateEnvelope)
async def calculate_wbs(req: WbsCalculateRequest) -> EstimateEnvelope:
    """Commit a WBS estimate: roll up, persist (Postgres history + Neo4j graph), retire the draft.

    Synchronous — the rollup is deterministic and fast, so unlike the twin flow there's no
    background task / SSE / interrupt. The frontend redirects to the existing review page."""
    from observability.correlation import bind_estimate_id

    estimate_id = str(uuid.uuid4())
    bind_estimate_id(estimate_id)
    # Normalize the roster BEFORE the rollup so the roster the estimate is costed against is the
    # SAME one persisted as wbs_stage2 — otherwise an empty-roster request rolls up against the
    # default team but saves the empty roster, and a later Duplicate re-rolls a different number.
    stage2 = req.stage2 or Stage2Context()
    if not stage2.roster.roles:
        stage2 = stage2.model_copy(update={"roster": RoleRoster.default()})
    req = req.model_copy(update={"stage2": stage2})

    # Identity is the fresh uuid above; the MC seed is the SAME stable value preview used, so
    # the committed numbers match what the user re-evaluated for this tree (see _stable_seed).
    final = await build_wbs_estimate(req, estimate_id=_stable_seed(req))
    env = EstimateEnvelope(
        estimate_id=estimate_id,
        project_name=req.project_name or "Untitled WBS estimate",
        status=EstimateStatus.COMPLETED,
        created_at=datetime.now(UTC),
        final_estimate=final,
        method="wbs",
        wbs_tree=req.tree,
        wbs_stage2=req.stage2,
        wbs_stage3=req.stage3,
    )
    runtime.register_envelope(estimate_id, env, evict=True)

    # Persist via the shared runtime seam — Postgres history + Neo4j snapshot + the WBS task
    # subgraph + calibration refresh, the SAME contract as the twin flow (no duplication). Run it
    # concurrently with retiring the source draft.
    persists: list = [
        runtime.persist_completed_estimate(env, stage2=req.stage2, stage3=req.stage3, wbs_tree=req.tree),
    ]
    if req.draft_id:
        persists.append(delete_wbs_draft(req.draft_id))
    await asyncio.gather(*persists)
    logger.info("WBS estimate %s committed (project=%r)", estimate_id, env.project_name)
    return env

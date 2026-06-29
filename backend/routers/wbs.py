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

from ag_ui.core import (
    CustomEvent,
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
)
from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

import runtime
from agents.wbs_agent import generate_wbs_tree, generate_wbs_tree_streamed
from agents.wbs_completeness import check_completeness
from agents.wbs_leaf_estimator import suggest_leaf_hours
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
from models.twin_outputs import DualScenarioEstimate, LlmUsage
from models.wbs_schema import (
    WbsCalculateRequest,
    WbsCompletenessRequest,
    WbsCompletenessResponse,
    WbsDraft,
    WbsDraftList,
    WbsDraftRequest,
    WbsDraftResponse,
    WbsDraftSaveRequest,
    WbsDraftSummary,
    WbsLeafHoursRequest,
    WbsLeafHoursSuggestion,
    WbsReconciliation,
    _sanitize_dependencies,
)
from models.wbs_task import WbsTaskInput, flatten_tree, rebuild_tree, regenerate_ids
from orchestrator.reconcile import parametric_estimate, reconcile
from orchestrator.usage import bind_usage_accumulator, capture_usage_to_db, summarize_usage
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
    llm_usage: LlmUsage | None = None,
) -> dict:
    """Flatten a draft into the Neo4j adapter's row shape (tree → flat task rows, context → JSON)."""
    return {
        "draft_id": draft_id,
        "project_name": project_name,
        "raw_input": raw_input,
        "stage2_json": stage2.model_dump_json() if stage2 else None,
        "stage3_json": stage3.model_dump_json() if stage3 else None,
        "contingency_pct": contingency_pct,
        "llm_usage_json": llm_usage.model_dump_json() if llm_usage else None,
        "tasks": flatten_tree(tree, draft_id),
    }


def _from_storage(data: dict) -> WbsDraft:
    """Rebuild a `WbsDraft` from the adapter's loaded rows."""
    draft_id = data["draft_id"]
    tree = rebuild_tree(data.get("tasks", []), draft_id)
    s2_json = data.get("stage2_json")
    s3_json = data.get("stage3_json")
    lu_json = data.get("llm_usage_json")
    return WbsDraft(
        draft_id=draft_id,
        project_name=data.get("project_name", "") or "",
        raw_input=data.get("raw_input", "") or "",
        tree=tree,
        stage2=Stage2Context.model_validate_json(s2_json) if s2_json else None,
        stage3=Stage3Context.model_validate_json(s3_json) if s3_json else None,
        contingency_pct=data.get("contingency_pct"),
        llm_usage=LlmUsage.model_validate_json(lu_json) if lu_json else None,
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
    # Re-sanitize: the duplicate path doesn't go through a request validator, so a source tree that
    # somehow carries a cross-kind/dangling depends_on edge would otherwise be cloned verbatim.
    _sanitize_dependencies(new_tree)
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
    # Capture the planner's Anthropic token cost (the LLM work that produced this draft) so the
    # editor can show it. Empty accumulator (no API key → deterministic fallback) ⇒ no usage.
    acc: list[dict] = []
    bind_usage_accumulator(acc)
    tree, notes = await generate_wbs_tree(req)
    usage = summarize_usage(acc) if acc else None
    draft_id = str(uuid.uuid4())
    await save_wbs_draft(
        _to_storage(
            draft_id, project_name=req.project_name or "", raw_input=req.raw_input,
            tree=tree, stage2=req.stage2, stage3=req.stage3, llm_usage=usage,
        ),
    )
    return WbsDraftResponse(draft_id=draft_id, tree=tree, notes=notes, llm_usage=usage)


def _extract_wbs_request(input_data: RunAgentInput) -> WbsDraftRequest:
    """Build a `WbsDraftRequest` from the AG-UI run's ``forwardedProps`` (same shape as the POST body).

    The frontend forwards ``{raw_input, project_name, stage2, stage3, selected_phases}``; model
    validation coerces stage2/stage3/phases and enforces the same constraints as the JSON endpoint.
    """
    props = input_data.forwarded_props if isinstance(input_data.forwarded_props, dict) else {}
    return WbsDraftRequest.model_validate(
        {
            "raw_input": str(props.get("raw_input") or ""),
            "project_name": props.get("project_name") or None,
            "stage2": props.get("stage2"),
            "stage3": props.get("stage3"),
            "selected_phases": props.get("selected_phases") or None,
        }
    )


def _wbs_progress_event(message: str) -> CustomEvent:
    """A friendly, human-readable WBS-draft progress event. The UI surfaces only the most recent one,
    so the user watches the planner work (reviewing → drafting each package + its tasks → finalizing)
    rather than staring at a frozen spinner."""
    return CustomEvent(type=EventType.CUSTOM, name="wbs_progress", value={"message": message})


async def wbs_agui_endpoint(input_data: RunAgentInput, request: Request) -> StreamingResponse:
    """AG-UI agent-run endpoint that streams the WBS planner as it drafts.

    Lifecycle: RUN_STARTED → CUSTOM('wbs_progress', {message}) narrating the planner's work
    (reviewing → drafting each work package + its tasks → finalizing) → STATE_SNAPSHOT (the persisted
    draft: id + tree + notes + llm_usage) → RUN_FINISHED (or RUN_ERROR on a catastrophic failure). The
    snapshot mirrors the POST /wbs/draft result so the frontend ends up with the same resumable draft,
    just with live, friendly progress messages.
    """
    encoder = EventEncoder(accept=request.headers.get("accept") or "text/event-stream")

    async def event_generator():
        yield encoder.encode(
            RunStartedEvent(
                type=EventType.RUN_STARTED,
                thread_id=input_data.thread_id,
                run_id=input_data.run_id,
            )
        )
        try:
            req = _extract_wbs_request(input_data)
            # Bind the usage accumulator BEFORE spawning the producer task so the task's copied
            # context shares the same list (record_usage appends to it; we summarize after).
            acc: list[dict] = []
            bind_usage_accumulator(acc)

            # A friendly opening status while the planner reads the brief + team (the model can take a
            # few seconds before the first package streams).
            yield encoder.encode(
                _wbs_progress_event("Reviewing your project description and proposed team…")
            )

            # Bridge the planner's sync per-node callback to this async generator with an unbounded
            # queue. The producer drafts the tree (never raises — it degrades to a real draft via the
            # non-streaming path), pushing ("event", message) as packages/tasks stream and a final
            # ("done", (tree, notes)). Counters/current-package live in dicts so the closure can mutate.
            queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
            counts = {"package": 0, "task": 0}
            current_pkg = {"name": ""}

            def _on_node(kind: str, name: str) -> None:
                clean = " ".join(name.split())[:120] or "…"
                if kind == "package":
                    counts["package"] += 1
                    current_pkg["name"] = clean
                    message = f"Planning work package {counts['package']}: {clean}"
                else:  # task
                    counts["task"] += 1
                    where = f" to {current_pkg['name']}" if current_pkg["name"] else ""
                    message = f"Adding task{where}: {clean}"
                queue.put_nowait(("event", message))

            async def _produce() -> None:
                try:
                    tree, notes = await generate_wbs_tree_streamed(req, on_node=_on_node)
                    queue.put_nowait(("done", (tree, notes)))
                except Exception as exc:  # noqa: BLE001 - surfaced as RUN_ERROR by the consumer
                    queue.put_nowait(("error", exc))

            task = asyncio.create_task(_produce())
            tree: list[WbsTaskInput] = []
            notes = ""
            try:
                while True:
                    kind, payload = await queue.get()
                    if kind == "event":
                        yield encoder.encode(_wbs_progress_event(str(payload)))
                    elif kind == "done":
                        tree, notes = payload  # type: ignore[assignment]
                        break
                    else:  # "error" — propagate to the RUN_ERROR handler below
                        raise payload  # type: ignore[misc]
            finally:
                if not task.done():
                    task.cancel()

            # Closing status while the deterministic rollup (complexity factor, backstop, persist) runs.
            yield encoder.encode(
                _wbs_progress_event("Balancing effort and finalizing your work breakdown…")
            )
            usage = summarize_usage(acc) if acc else None
            draft_id = str(uuid.uuid4())
            await save_wbs_draft(
                _to_storage(
                    draft_id, project_name=req.project_name or "", raw_input=req.raw_input,
                    tree=tree, stage2=req.stage2, stage3=req.stage3, llm_usage=usage,
                ),
            )
            snapshot = {
                "draft_id": draft_id,
                "tree": [t.model_dump(mode="json") for t in tree],
                "notes": notes,
                "llm_usage": usage.model_dump(mode="json") if usage else None,
            }
            yield encoder.encode(
                StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=snapshot)
            )
            logger.info(
                "WBS AG-UI run finished (%d package(s), %d task(s), draft=%s)",
                counts["package"], counts["task"], draft_id,
            )
            yield encoder.encode(
                RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id=input_data.thread_id,
                    run_id=input_data.run_id,
                )
            )
        except Exception:  # noqa: BLE001 - never leak internal LLM/DB details to the client
            logger.exception("WBS AG-UI run failed; emitting RUN_ERROR")
            yield encoder.encode(
                RunErrorEvent(type=EventType.RUN_ERROR, message="wbs draft failed")
            )

    return StreamingResponse(event_generator(), media_type=encoder.get_content_type())


# AG-UI agent-run endpoint for the streaming WBS draft. Registered via add_api_route (like the
# roster one) so the handler's RunAgentInput body + Request signature drives FastAPI parsing.
router.add_api_route("/wbs/draft/agui", wbs_agui_endpoint, methods=["POST"])


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
    """Autosave the editor state for a draft (idempotent rebuild-on-save).

    The response is a save-echo of the *mutable* editor fields. Server-owned fields the save request
    doesn't carry — ``llm_usage`` (set once at draft time) and ``created_at``/``updated_at`` — are
    intentionally omitted here rather than re-read on every debounced autosave; they're preserved in
    Neo4j (``save_wbs_draft`` uses ``coalesce`` so the absent ``llm_usage`` isn't wiped) and remain
    available via ``GET /wbs/drafts/{id}``, which the editor reads at mount. Don't treat this echo as
    the authoritative full draft.
    """
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
        # Carry the original description so the clone keeps its context (and a later re-draft has prose
        # to plan from). Older envelopes persisted before wbs_raw_input existed fall back to "".
        name=env.project_name, raw_input=env.wbs_raw_input or "", contingency_pct=contingency,
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


@router.post("/estimates/wbs/reconcile", response_model=WbsReconciliation)
async def reconcile_wbs(req: WbsCalculateRequest) -> WbsReconciliation:
    """Triangulate the bottom-up WBS rollup against a parametric (twin) estimate of the SAME brief
    and return the per-phase + total divergence — a sanity check for omitted work (WBS below
    parametric) or double-counting (WBS above). On-demand (explicit user action): runs the six twins'
    Pass-1 + parse_input (~7 LLM calls), so it's a separate button, not part of Re-evaluate. The
    parametric token cost is returned in the response AND persisted to `llm_call` so it shows in global
    Observability (stamped with the wizard `session_id` to reparent onto the estimate on commit).
    Degrades to a structural-only comparison (``parametric_available=false``) with no API key."""
    from config import get_settings
    from observability.correlation import bind_estimate_id

    seed = _stable_seed(req)
    bind_estimate_id(f"wbs-reconcile:{seed}")
    wbs = await build_wbs_estimate(req, estimate_id=seed)

    # `capture_usage_to_db` binds the accumulator, persists the parametric twins' per-call cost to
    # `llm_call` (no estimate id yet — the wizard `session_id` reparents it onto the estimate on
    # commit), and never-raises; we `summarize_usage` the yielded accumulator for the response too.
    async with capture_usage_to_db(session_id=req.session_id) as acc:
        parametric = await parametric_estimate(req)
        return reconcile(
            wbs,
            parametric,
            parametric_available=bool(get_settings().anthropic_api_key),
            llm_usage=summarize_usage(acc) if acc else None,
        )


@router.post("/estimates/wbs/completeness", response_model=WbsCompletenessResponse)
async def wbs_completeness(req: WbsCompletenessRequest) -> WbsCompletenessResponse:
    """Audit the tree for OMITTED work — the editor's "Check completeness" button. Catches WITHIN-phase
    omission (a present phase missing a specific task, e.g. no data-migration/security-review) that the
    totals-only reconciliation can't see. One streamed LLM call (cheaper than Reconcile); the token
    cost is returned AND persisted to `llm_call` so it shows in global Observability. Degrades to an
    empty result (no findings) without an API key — never errors."""
    # capture_usage_to_db binds + persists the call cost (reparented onto the estimate on commit via
    # the wizard session_id) and yields the accumulator so we also surface it on the response.
    async with capture_usage_to_db(session_id=req.session_id) as acc:
        resp = await check_completeness(req)
        resp.llm_usage = summarize_usage(acc) if acc else None
        return resp


@router.post("/estimates/wbs/suggest-hours", response_model=WbsLeafHoursSuggestion)
async def wbs_suggest_hours(req: WbsLeafHoursRequest) -> WbsLeafHoursSuggestion:
    """Suggest a 3-point estimate for ONE leaf — the editor's per-task "Suggest hours" button (#5c).
    Grounded in the brief + the leaf's package/siblings so it stays proportionate to the rest of the
    tree. The token cost is returned AND persisted to `llm_call` (global Observability), like the
    completeness critic. Degrades to `available=false` (no suggestion) without an API key — never
    errors."""
    # Same usage capture+persist seam as completeness/reconcile (see capture_usage_to_db).
    async with capture_usage_to_db(session_id=req.session_id) as acc:
        resp = await suggest_leaf_hours(req)
        resp.llm_usage = summarize_usage(acc) if acc else None
        return resp


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
    # Carry the planner-draft LLM cost onto the final estimate so it surfaces in the global
    # observability view (the deterministic rollup spends no tokens of its own).
    if req.llm_usage is not None:
        final = final.model_copy(update={"llm_usage": req.llm_usage})
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
        wbs_raw_input=req.raw_input or None,
    )
    runtime.register_envelope(estimate_id, env, evict=True)

    # Persist via the shared runtime seam — Postgres history + Neo4j snapshot + the WBS task
    # subgraph + calibration refresh, the SAME contract as the twin flow (no duplication). Run it
    # concurrently with retiring the source draft.
    persists: list = [
        runtime.persist_completed_estimate(
            env, stage2=req.stage2, stage3=req.stage3, wbs_tree=req.tree, session_id=req.session_id
        ),
    ]
    if req.draft_id:
        persists.append(delete_wbs_draft(req.draft_id))
    await asyncio.gather(*persists)
    logger.info("WBS estimate %s committed (project=%r)", estimate_id, env.project_name)
    return env

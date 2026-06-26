"""Neo4j driver lifecycle + LangGraph checkpointer factory.

MVP note: LangGraph does not yet ship an official Neo4j checkpointer in core.
We use `InMemorySaver` for the in-process graph state and persist a denormalized
copy of the final estimate (plus per-phase nodes) to Neo4j via `save_estimate_envelope()`
for the calibration / history features. This lets us swap in a real
`Neo4jCheckpointSaver` later without changing call sites.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from neo4j import AsyncGraphDatabase

from config import get_settings

if TYPE_CHECKING:
    from neo4j import AsyncDriver

logger = logging.getLogger(__name__)

_driver: AsyncDriver | None = None
# Serializes the one-time driver construction so concurrent first-callers don't each build a driver
# (the loser would leak, never closed). AsyncDriver itself is safe for concurrent coroutines.
_driver_lock = asyncio.Lock()


async def get_driver() -> AsyncDriver | None:
    """Return a cached async Neo4j driver, or None if Neo4j is unreachable.

    The backend should keep working even if Neo4j is down (estimates just won't persist).
    """
    global _driver
    if _driver is not None:
        return _driver

    settings = get_settings()
    if not settings.neo4j_password:
        logger.warning("NEO4J_PASSWORD not set; persistence disabled")
        return None

    async with _driver_lock:
        if _driver is not None:  # another coroutine connected while we waited for the lock
            return _driver
        try:
            driver = AsyncGraphDatabase.driver(
                settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
            )
            await driver.verify_connectivity()
            _driver = driver
            logger.info("Connected to Neo4j at %s", settings.neo4j_uri)
            return _driver
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j connect failed (%s); persistence disabled", exc)
            _driver = None
            return None


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def save_estimate_envelope(envelope: dict[str, Any]) -> None:
    """Persist a denormalized snapshot of an estimate to Neo4j.

    Idempotent on `estimate_id` — calling twice updates rather than duplicates.
    """
    driver = await get_driver()
    if driver is None:
        logger.debug(
            "neo4j: skipping save for estimate %s (driver unavailable / NEO4J_PASSWORD unset)",
            envelope.get("estimate_id"),
        )
        return

    cypher = """
    MERGE (e:Estimate {id: $estimate_id})
    SET e.project_name = $project_name,
        e.status = $status,
        e.updated_at = datetime($updated_at),
        e.raw_input = $raw_input
    WITH e
    UNWIND $phases AS phase
      MERGE (p:Phase {estimate_id: $estimate_id, name: phase.phase})
      SET p.twin_name = phase.twin_name,
          p.algorithm = phase.algorithm,
          p.ai_assisted_mid = phase.ai_mid,
          p.manual_only_mid = phase.manual_mid,
          p.confidence = phase.confidence
      MERGE (e)-[:INCLUDES_PHASE]->(p)
    """
    settings = get_settings()
    phases = envelope.get("phases", [])
    try:
        async with driver.session(database=settings.neo4j_database) as session:
            await session.run(
                cypher,
                estimate_id=envelope["estimate_id"],
                project_name=envelope.get("project_name", ""),
                status=envelope.get("status", "unknown"),
                updated_at=datetime.now(UTC).isoformat(),
                raw_input=(envelope.get("raw_input") or "")[:5000],
                phases=phases,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "neo4j: save failed for estimate %s (%s); skipping",
            envelope.get("estimate_id"),
            exc,
        )
        return
    logger.info(
        "neo4j: saved estimate %s (%d phase node(s))",
        envelope["estimate_id"],
        len(phases),
    )


# --------------------------------------------------------------------------------------
# WBS (Work Breakdown Structure) graph storage.
#
# The bottom-up WBS flow stores its task hierarchy graph-natively: a (:WbsDraft) or
# (:Estimate) owner node with a (:WbsTask)-[:HAS_CHILD]->(:WbsTask) subgraph hanging off it.
# These are the adapter's FIRST read functions — they honor the same never-raise contract as
# the writes (return None / [] on driver-unavailable or Cypher error, never raise), so the WBS
# feature degrades to client localStorage when Neo4j is down. Task rows are plain dicts
# (flattened by models.wbs_task.flatten_tree) so the adapter stays model-free, like
# save_estimate_envelope above.
# --------------------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    """Best-effort ISO string for a neo4j temporal (or anything) read back from the graph."""
    if value is None:
        return None
    for attr in ("isoformat", "iso_format"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                break
    return str(value)


async def _replace_task_subgraph(tx: Any, owner_id: str, tasks: list[dict[str, Any]]) -> None:
    """Wipe this owner's existing :WbsTask subgraph and rebuild it from flat rows.

    Runs on a managed-transaction handle (``tx``) so the wipe + rebuild + link statements are
    one atomic unit with the owner-node MERGE the caller issues on the same ``tx`` — a mid-write
    failure rolls the whole thing back rather than leaving an owner node with a half-built (or
    empty) subgraph.

    Idempotent rebuild-on-save (mirrors the Postgres phase-row replace). Nodes are keyed on
    the composite ``(owner_id, task_id)`` so a draft and the estimate it commits into can carry
    identical task_ids without colliding. Each row carries ``parent_id`` (the owner id for
    top-level nodes, else the parent task id) and sibling ``order``; the task→task HAS_CHILD
    edges are built here, the owner→top-level edges by the caller (draft vs estimate root)."""
    await tx.run("MATCH (n:WbsTask {owner_id: $owner_id}) DETACH DELETE n", owner_id=owner_id)
    if not tasks:
        return
    await tx.run(
        """
        UNWIND $tasks AS t
          CREATE (n:WbsTask {owner_id: $owner_id, task_id: t.task_id})
          SET n.name = t.name, n.description = t.description, n.phase = t.phase,
              n.role_id = t.role_id, n.is_leaf = t.is_leaf, n.order = t.order,
              n.parent_id = t.parent_id, n.optimistic = t.optimistic,
              n.most_likely = t.most_likely, n.pessimistic = t.pessimistic,
              n.depends_on = t.depends_on
        """,
        owner_id=owner_id,
        tasks=tasks,
    )
    await tx.run(
        """
        UNWIND $tasks AS t
          WITH t WHERE t.parent_id <> $owner_id
          MATCH (p:WbsTask {owner_id: $owner_id, task_id: t.parent_id})
          MATCH (c:WbsTask {owner_id: $owner_id, task_id: t.task_id})
          MERGE (p)-[:HAS_CHILD]->(c)
        """,
        owner_id=owner_id,
        tasks=tasks,
    )


async def _write_draft_tx(
    tx: Any,
    *,
    draft_id: str,
    project_name: str,
    raw_input: str,
    stage2_json: str | None,
    stage3_json: str | None,
    contingency_pct: float | None,
    llm_usage_json: str | None,
    tasks: list[dict[str, Any]],
    now: str,
) -> None:
    """Atomic unit of work for ``save_wbs_draft``: owner MERGE + subgraph rebuild + top-level links.

    All three statements run on the same managed transaction so the draft node is never persisted
    without its tasks (and vice versa) — the partial-persist failure mode that left
    ``load_wbs_draft`` returning an empty tree instead of cleanly 404-ing."""
    await tx.run(
        """
        MERGE (d:WbsDraft {draft_id: $draft_id})
        SET d.project_name = $project_name, d.raw_input = $raw_input,
            d.stage2_json = $stage2_json, d.stage3_json = $stage3_json,
            d.contingency_pct = $contingency_pct,
            d.llm_usage_json = coalesce($llm_usage_json, d.llm_usage_json),
            d.task_count = $task_count, d.updated_at = datetime($now),
            d.created_at = coalesce(d.created_at, datetime($now))
        """,
        draft_id=draft_id,
        project_name=project_name,
        raw_input=raw_input,
        stage2_json=stage2_json,
        stage3_json=stage3_json,
        contingency_pct=contingency_pct,
        llm_usage_json=llm_usage_json,
        task_count=len(tasks),
        now=now,
    )
    await _replace_task_subgraph(tx, draft_id, tasks)
    await tx.run(
        """
        MATCH (d:WbsDraft {draft_id: $draft_id})
        UNWIND $tasks AS t
          WITH d, t WHERE t.parent_id = $draft_id
          MATCH (c:WbsTask {owner_id: $draft_id, task_id: t.task_id})
          MERGE (d)-[:HAS_CHILD]->(c)
        """,
        draft_id=draft_id,
        tasks=tasks,
    )


async def _write_tree_tx(tx: Any, *, owner_id: str, tasks: list[dict[str, Any]]) -> None:
    """Atomic unit of work for ``save_wbs_tree``: subgraph rebuild + estimate→top-level links.

    One managed transaction so a committed estimate never ends up with a half-attached subgraph."""
    await _replace_task_subgraph(tx, owner_id, tasks)
    await tx.run(
        """
        MATCH (e:Estimate {id: $owner_id})
        UNWIND $tasks AS t
          WITH e, t WHERE t.parent_id = $owner_id
          MATCH (c:WbsTask {owner_id: $owner_id, task_id: t.task_id})
          MERGE (e)-[:HAS_CHILD]->(c)
        """,
        owner_id=owner_id,
        tasks=tasks,
    )


async def save_wbs_draft(draft: dict[str, Any]) -> None:
    """Persist a resumable WBS draft as a (:WbsDraft) node + its :WbsTask subgraph.

    ``draft`` carries ``draft_id``, ``project_name``, ``raw_input``, ``stage2_json`` /
    ``stage3_json`` (serialized context), and ``tasks`` (flat rows from ``flatten_tree``).
    Idempotent on ``draft_id``. Silently no-ops when Neo4j is unavailable.
    """
    driver = await get_driver()
    draft_id = str(draft.get("draft_id") or "")
    if driver is None:
        logger.debug("neo4j: skipping WBS draft save for %s (driver unavailable)", draft_id)
        return
    settings = get_settings()
    tasks = draft.get("tasks", [])
    now = datetime.now(UTC).isoformat()
    try:
        async with driver.session(database=settings.neo4j_database) as session:
            await session.execute_write(
                _write_draft_tx,
                draft_id=draft_id,
                project_name=draft.get("project_name", ""),
                raw_input=(draft.get("raw_input", "") or "")[:20000],
                stage2_json=draft.get("stage2_json"),
                stage3_json=draft.get("stage3_json"),
                contingency_pct=draft.get("contingency_pct"),
                llm_usage_json=draft.get("llm_usage_json"),
                tasks=tasks,
                now=now,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("neo4j: WBS draft save failed for %s (%s); skipping", draft_id, exc)
        return
    logger.info("neo4j: saved WBS draft %s (%d task node(s))", draft_id, len(tasks))


async def load_wbs_draft(draft_id: str) -> dict[str, Any] | None:
    """Load a WBS draft (root props + flat task rows) for resume, or None if absent / off."""
    driver = await get_driver()
    if driver is None:
        return None
    settings = get_settings()
    try:
        async with driver.session(database=settings.neo4j_database) as session:
            result = await session.run(
                """
                MATCH (d:WbsDraft {draft_id: $draft_id})
                OPTIONAL MATCH (d)-[:HAS_CHILD*]->(t:WbsTask)
                RETURN d AS draft, collect(t) AS tasks
                """,
                draft_id=draft_id,
            )
            record = await result.single()
    except Exception as exc:  # noqa: BLE001
        logger.warning("neo4j: WBS draft load failed for %s (%s)", draft_id, exc)
        return None
    if record is None or record["draft"] is None:
        return None
    d = dict(record["draft"])
    tasks = [dict(t) for t in record["tasks"] if t is not None]
    # NOTE: a task-less draft is returned as-is (empty tree). Writes are atomic (save_wbs_draft
    # runs in one managed transaction), so a task-less node is a legitimately-emptied draft — the
    # user deleted every task — not a partial write. Treating it as "not found" would brick a
    # draft the user can no longer resume.
    return {
        "draft_id": d.get("draft_id"),
        "project_name": d.get("project_name", ""),
        "raw_input": d.get("raw_input", ""),
        "stage2_json": d.get("stage2_json"),
        "stage3_json": d.get("stage3_json"),
        "contingency_pct": d.get("contingency_pct"),
        "llm_usage_json": d.get("llm_usage_json"),
        "created_at": _iso(d.get("created_at")),
        "updated_at": _iso(d.get("updated_at")),
        "tasks": tasks,
    }


async def list_wbs_drafts(limit: int = 50) -> list[dict[str, Any]]:
    """Resume list: WBS draft summaries newest-first. [] when absent / Neo4j off."""
    driver = await get_driver()
    if driver is None:
        return []
    settings = get_settings()
    try:
        async with driver.session(database=settings.neo4j_database) as session:
            result = await session.run(
                # task_count is maintained on the node at save time, so read it directly rather
                # than expanding the [:HAS_CHILD*] subgraph of every draft just to recount.
                """
                MATCH (d:WbsDraft)
                RETURN d.draft_id AS draft_id, d.project_name AS project_name,
                       d.updated_at AS updated_at, coalesce(d.task_count, 0) AS task_count
                ORDER BY d.updated_at DESC
                LIMIT $limit
                """,
                limit=limit,
            )
            return [
                {
                    "draft_id": r["draft_id"],
                    "project_name": r["project_name"] or "",
                    "updated_at": _iso(r["updated_at"]),
                    "task_count": r["task_count"] or 0,
                }
                async for r in result
            ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("neo4j: WBS draft list failed (%s)", exc)
        return []


async def delete_wbs_draft(draft_id: str) -> None:
    """Remove a WBS draft + its subgraph. Idempotent; silently no-ops when Neo4j is off."""
    driver = await get_driver()
    if driver is None:
        return
    settings = get_settings()
    try:
        async with driver.session(database=settings.neo4j_database) as session:
            await session.run(
                """
                MATCH (d:WbsDraft {draft_id: $draft_id})
                OPTIONAL MATCH (d)-[:HAS_CHILD*]->(t:WbsTask)
                DETACH DELETE d, t
                """,
                draft_id=draft_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("neo4j: WBS draft delete failed for %s (%s)", draft_id, exc)


async def save_wbs_tree(owner_id: str, tasks: list[dict[str, Any]]) -> None:
    """Attach a committed WBS task subgraph under its existing (:Estimate {id}) node.

    Called on WBS commit so the finalized hierarchy lives in the graph alongside the estimate
    snapshot written by ``save_estimate_envelope``. Silently no-ops when Neo4j is off."""
    driver = await get_driver()
    if driver is None:
        logger.debug("neo4j: skipping WBS tree save for estimate %s (driver unavailable)", owner_id)
        return
    settings = get_settings()
    try:
        async with driver.session(database=settings.neo4j_database) as session:
            await session.execute_write(_write_tree_tx, owner_id=owner_id, tasks=tasks)
    except Exception as exc:  # noqa: BLE001
        logger.warning("neo4j: WBS tree save failed for estimate %s (%s); skipping", owner_id, exc)
        return
    logger.info("neo4j: saved WBS tree for estimate %s (%d task node(s))", owner_id, len(tasks))


def _checkpoint_serde() -> Any:
    """JsonPlus serializer with our state models on the msgpack allowlist.

    LangGraph's msgpack serde warns on (and will eventually block) deserializing
    custom types that aren't registered. Every custom type we put in the graph
    state lives in `models.twin_outputs` / `models.project_schema` /
    `models.estimation_state`, so we register every class defined in those modules.
    This silences the warning AND adopts the explicit allowlist the warning
    recommends, without hand-maintaining a list — new models in those modules are
    picked up automatically. LangGraph's own checkpointed types are already covered
    by its built-in SAFE_MSGPACK_TYPES, so passing our classes is sufficient.
    """
    import inspect

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from models import estimation_state, project_schema, twin_outputs

    allow: list[type] = [
        obj
        for mod in (twin_outputs, project_schema, estimation_state)
        for _, obj in inspect.getmembers(mod, inspect.isclass)
        if obj.__module__ == mod.__name__  # defined here, not imported
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allow)


def make_checkpointer() -> Any:
    """Return a LangGraph checkpointer.

    MVP returns `InMemorySaver`; survives within a process but not across restarts.
    TODO: swap to a Neo4j-backed BaseCheckpointSaver implementation in Phase 3.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver(serde=_checkpoint_serde())

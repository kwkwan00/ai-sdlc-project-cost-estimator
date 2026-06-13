"""Neo4j driver lifecycle + LangGraph checkpointer factory.

MVP note: LangGraph does not yet ship an official Neo4j checkpointer in core.
We use `InMemorySaver` for the in-process graph state and persist a denormalized
copy of the final estimate (plus per-phase nodes) to Neo4j via `save_estimate()`
for the calibration / history features. This lets us swap in a real
`Neo4jCheckpointSaver` later without changing call sites.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from neo4j import GraphDatabase

from config import get_settings

if TYPE_CHECKING:
    from neo4j import Driver

logger = logging.getLogger(__name__)

_driver: Driver | None = None


def get_driver() -> Driver | None:
    """Return a cached Neo4j driver, or None if Neo4j is unreachable.

    The backend should keep working even if Neo4j is down (estimates just won't persist).
    """
    global _driver
    if _driver is not None:
        return _driver

    settings = get_settings()
    if not settings.neo4j_password:
        logger.warning("NEO4J_PASSWORD not set; persistence disabled")
        return None

    try:
        _driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )
        _driver.verify_connectivity()
        logger.info("Connected to Neo4j at %s", settings.neo4j_uri)
        return _driver
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j connect failed (%s); persistence disabled", exc)
        _driver = None
        return None


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def save_estimate_envelope(envelope: dict[str, Any]) -> None:
    """Persist a denormalized snapshot of an estimate to Neo4j.

    Idempotent on `estimate_id` — calling twice updates rather than duplicates.
    """
    driver = get_driver()
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
        with driver.session(database=settings.neo4j_database) as session:
            session.run(
                cypher,
                estimate_id=envelope["estimate_id"],
                project_name=envelope.get("project_name", ""),
                status=envelope.get("status", "unknown"),
                updated_at=datetime.now(UTC).isoformat(),
                raw_input=envelope.get("raw_input", "")[:5000],
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

"""Qdrant client + the vector-similarity calibration store.

Qdrant is a **secondary, derived index** over data Postgres/Neo4j already own — completed estimates,
their per-phase outcomes, their WBS leaf tasks, and the clarifying questions they raised — embedded so
a new estimate can retrieve the semantically nearest past cases (reference-class forecasting). It is
**purely additive**: nothing here replaces the Neo4j envelope snapshots or the Postgres history.

Like every other store, it **degrades silently**: when Qdrant is unreachable (or embeddings are
unavailable upstream) the writes/reads no-op and the rest of the backend keeps serving. Public
functions are ``async`` and wrap the sync ``qdrant_client`` in ``asyncio.to_thread`` so the event loop
is never blocked. Never raises.

Collections (all vectors are cosine, ``EMBED_DIMS`` wide):
- ``reference_cases``   — one point per completed estimate (brief+context → realized totals).
- ``phase_cases``       — one point per (estimate × phase) (brief+phase → that phase's realized hours).
- ``wbs_tasks``         — one point per WBS leaf task (name+description → its 3-point hours).
- ``clarifying_questions`` — one point per clarifying question the twins raised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from config import get_settings
from orchestrator.embeddings import EMBED_DIMS

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)

_client: QdrantClient | None = None

# Collection names — the four calibration data types.
REFERENCE_CASES = "reference_cases"
PHASE_CASES = "phase_cases"
WBS_TASKS = "wbs_tasks"
CLARIFYING_QUESTIONS = "clarifying_questions"
ALL_COLLECTIONS = (REFERENCE_CASES, PHASE_CASES, WBS_TASKS, CLARIFYING_QUESTIONS)


def get_client() -> QdrantClient | None:
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    logger.debug("Qdrant client init + collection bootstrap at %s", settings.qdrant_url)
    try:
        from qdrant_client import QdrantClient

        _client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
        # Healthcheck — `get_collections` is cheap and proves connectivity.
        _client.get_collections()
        logger.info("Connected to Qdrant at %s", settings.qdrant_url)
        return _client
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant connect failed (%s); vector calibration disabled", exc)
        _client = None
        return None


def close_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # noqa: BLE001
            pass
        _client = None


def _set_client_for_tests(client: QdrantClient | None) -> None:
    """Inject an in-memory ``QdrantClient(':memory:')`` (or None) for tests, bypassing the network."""
    global _client
    _client = client


async def ensure_collections() -> bool:
    """Create the four collections if absent (idempotent). Returns False when Qdrant is unavailable.
    Best-effort: called on startup, but the write path also tolerates missing collections."""
    client = get_client()
    if client is None:
        return False

    def _ensure() -> bool:
        from qdrant_client import models

        for name in ALL_COLLECTIONS:
            if not client.collection_exists(name):
                client.create_collection(
                    name,
                    vectors_config=models.VectorParams(
                        size=EMBED_DIMS, distance=models.Distance.COSINE
                    ),
                )
        return True

    try:
        return await asyncio.to_thread(_ensure)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant ensure_collections failed (%s); vector calibration disabled", exc)
        return False


async def upsert(collection: str, points: list[dict[str, Any]]) -> None:
    """Upsert points ``[{id, vector, payload}, ...]`` into a collection. No-op when Qdrant is off or
    ``points`` is empty. Ensures the collection exists first. Never raises."""
    if not points:
        return
    client = get_client()
    if client is None:
        return

    def _upsert() -> None:
        from qdrant_client import models

        if not client.collection_exists(collection):
            client.create_collection(
                collection,
                vectors_config=models.VectorParams(size=EMBED_DIMS, distance=models.Distance.COSINE),
            )
        client.upsert(
            collection_name=collection,
            points=[
                models.PointStruct(id=p["id"], vector=p["vector"], payload=p.get("payload", {}))
                for p in points
            ],
            wait=True,
        )

    try:
        await asyncio.to_thread(_upsert)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant upsert into %s failed (%s); skipping", collection, exc)


async def search(
    collection: str,
    vector: list[float],
    *,
    limit: int = 5,
    must_match: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the ``limit`` nearest points as ``[{score, payload}, ...]`` (closest first). ``must_match``
    is an optional exact-match payload filter (e.g. ``{"phase": "development"}``). Returns ``[]`` when
    Qdrant is off / the collection is missing / on any error. Never raises."""
    client = get_client()
    if client is None:
        return []

    def _search() -> list[dict[str, Any]]:
        from qdrant_client import models

        if not client.collection_exists(collection):
            return []
        query_filter = None
        if must_match:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(key=k, match=models.MatchValue(value=v))
                    for k, v in must_match.items()
                ]
            )
        result = client.query_points(
            collection_name=collection,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return [{"score": p.score, "payload": p.payload or {}} for p in result.points]

    try:
        return await asyncio.to_thread(_search)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant search in %s failed (%s); returning no neighbors", collection, exc)
        return []

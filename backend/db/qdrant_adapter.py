"""Qdrant client + collection bootstrap. Scaffolded for Phase 3 calibration; not used in MVP."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config import get_settings

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)

_client: QdrantClient | None = None


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
        logger.warning("Qdrant connect failed (%s); calibration disabled", exc)
        _client = None
        return None


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None

"""Text embedding helper — the single seam for turning text into vectors for Qdrant.

Reuses the shared OpenAI client (`llm._get_openai_client`, same provider the eval judge uses) and
pins the vector size to ``EMBED_DIMS`` via the OpenAI ``dimensions`` param so the Qdrant collections
have a stable width regardless of the configured model. Best-effort: returns ``None`` when there's no
``OPENAI_API_KEY`` / on empty input / on any API failure, so callers (estimate indexing, similarity
search) simply skip Qdrant rather than failing the request.
"""

from __future__ import annotations

import logging

from config import get_settings

logger = logging.getLogger(__name__)

# Pinned vector width. text-embedding-3-small is natively 1536; the OpenAI `dimensions` param lets
# 3-large (or a future model) be truncated to the same width, so the collections never need recreating.
EMBED_DIMS = 1536


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of strings into ``EMBED_DIMS``-wide vectors (one OpenAI call). Returns ``None``
    when embeddings are unavailable (no key / failure) so the caller skips Qdrant; otherwise returns a
    list aligned 1:1 with ``texts``."""
    cleaned = [t if t.strip() else " " for t in texts]
    if not cleaned:
        return []
    settings = get_settings()
    if not settings.openai_api_key:
        return None
    try:
        from orchestrator.llm import _get_openai_client

        client = _get_openai_client()
        resp = await client.embeddings.create(
            model=settings.embedding_model, input=cleaned, dimensions=EMBED_DIMS
        )
        return [item.embedding for item in resp.data]
    except Exception as exc:  # noqa: BLE001
        logger.warning("embedding failed (%s); skipping Qdrant for this batch", exc)
        return None


async def embed_text(text: str) -> list[float] | None:
    """Embed a single string (convenience over `embed_texts`). ``None`` when unavailable."""
    vectors = await embed_texts([text])
    return vectors[0] if vectors else None

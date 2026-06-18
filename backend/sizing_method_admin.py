"""Generic admin surface for a single-choice twin "sizing method" app setting.

A twin's sizing algorithm (e.g. Development COCOMO↔FP, QA TPA↔TCPA) is a global choice stored in
the ``app_settings`` KV table. This module owns the shared read/validate/persist logic; the
per-twin admin modules (``dev_sizing_admin``, ``qa_sizing_admin``) are thin wrappers that bind
their setting key, code default, and allowed choices. Writes degrade gracefully: when Postgres is
disabled the GET still returns the code default (read-only) and the PUT reports it wasn't persisted
(``editable=false``). Mirrors the other admin surfaces.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from config import get_settings
from db.repositories import get_app_setting, set_app_setting

logger = logging.getLogger(__name__)


class SizingMethodResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # True when Postgres is connected and edits will persist.
    editable: bool
    method: str
    default_method: str
    methods: list[str]


class SizingMethodUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str


async def get_sizing_method(
    key: str, default: str, methods: tuple[str, ...]
) -> SizingMethodResponse:
    """The current selected method for ``key`` (DB → code default), plus the allowed choices."""
    method = await get_app_setting(key, default)
    return SizingMethodResponse(
        editable=get_settings().postgres_enabled,
        method=method,
        default_method=default,
        methods=list(methods),
    )


async def update_sizing_method(
    key: str, default: str, methods: tuple[str, ...], update: SizingMethodUpdate
) -> SizingMethodResponse:
    """Validate + persist the chosen method for ``key``, then return the new effective state."""
    if update.method not in methods:
        raise HTTPException(
            422, f"method must be one of {list(methods)} (got {update.method!r})"
        )
    persisted = await set_app_setting(key, update.method)
    if not persisted:
        logger.warning("%s update not persisted (Postgres disabled/failed)", key)
    return await get_sizing_method(key, default, methods)

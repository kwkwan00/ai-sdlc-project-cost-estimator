"""Admin surface for the Discovery sizing method (Use Case Points ↔ FP-based analysis effort).

A thin binding over the generic ``sizing_method_admin`` helper: the global app setting
``discovery_sizing_method`` in the ``app_settings`` KV table switches the Discovery twin between
Use Case Points (default) and FP-based analysis effort. See ``sizing_method_admin`` for the shared
read/validate/persist + ``editable=false`` semantics.
"""

from __future__ import annotations

from admin.sizing_method_admin import (
    SizingMethodResponse as DiscoverySizingResponse,
)
from admin.sizing_method_admin import (
    SizingMethodUpdate as DiscoverySizingUpdate,
)
from admin.sizing_method_admin import (
    get_sizing_method,
    update_sizing_method,
)
from orchestrator.nodes.discovery_analyst import (
    DEFAULT_DISCOVERY_SIZING_METHOD,
    DISCOVERY_SIZING_METHODS,
)

__all__ = [
    "DiscoverySizingResponse",
    "DiscoverySizingUpdate",
    "get_discovery_sizing_method",
    "update_discovery_sizing_method",
]

_SETTING_KEY = "discovery_sizing_method"


async def get_discovery_sizing_method() -> DiscoverySizingResponse:
    return await get_sizing_method(
        _SETTING_KEY, DEFAULT_DISCOVERY_SIZING_METHOD, DISCOVERY_SIZING_METHODS
    )


async def update_discovery_sizing_method(
    update: DiscoverySizingUpdate,
) -> DiscoverySizingResponse:
    return await update_sizing_method(
        _SETTING_KEY, DEFAULT_DISCOVERY_SIZING_METHOD, DISCOVERY_SIZING_METHODS, update
    )

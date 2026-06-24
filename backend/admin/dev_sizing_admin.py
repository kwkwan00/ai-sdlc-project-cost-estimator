"""Admin surface for the Development sizing method (COCOMO II ↔ Function Points).

A thin binding over the generic ``sizing_method_admin`` helper: the global app setting
``development_sizing_method`` in the ``app_settings`` KV table switches the Development twin
between COCOMO II (default) and IFPUG Function Points. See ``sizing_method_admin`` for the shared
read/validate/persist + ``editable=false`` semantics.
"""

from __future__ import annotations

from admin.sizing_method_admin import (
    SizingMethodResponse as DevSizingResponse,
)
from admin.sizing_method_admin import (
    SizingMethodUpdate as DevSizingUpdate,
)
from admin.sizing_method_admin import (
    get_sizing_method,
    update_sizing_method,
)
from orchestrator.nodes.development_architect import (
    DEFAULT_DEV_SIZING_METHOD,
    DEV_SIZING_METHODS,
)

__all__ = [
    "DevSizingResponse",
    "DevSizingUpdate",
    "get_dev_sizing_method",
    "update_dev_sizing_method",
]

_SETTING_KEY = "development_sizing_method"


async def get_dev_sizing_method() -> DevSizingResponse:
    return await get_sizing_method(_SETTING_KEY, DEFAULT_DEV_SIZING_METHOD, DEV_SIZING_METHODS)


async def update_dev_sizing_method(update: DevSizingUpdate) -> DevSizingResponse:
    return await update_sizing_method(
        _SETTING_KEY, DEFAULT_DEV_SIZING_METHOD, DEV_SIZING_METHODS, update
    )

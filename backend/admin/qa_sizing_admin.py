"""Admin surface for the QA/Testing sizing method (TPA ↔ Test Case Point Analysis).

A thin binding over the generic ``sizing_method_admin`` helper: the global app setting
``qa_sizing_method`` in the ``app_settings`` KV table switches the QA twin between TPA (default —
function-point-driven test points) and Test Case Point Analysis (test-case-count driven). See
``sizing_method_admin`` for the shared read/validate/persist + ``editable=false`` semantics.
"""

from __future__ import annotations

from admin.sizing_method_admin import (
    SizingMethodResponse as QaSizingResponse,
)
from admin.sizing_method_admin import (
    SizingMethodUpdate as QaSizingUpdate,
)
from admin.sizing_method_admin import (
    get_sizing_method,
    update_sizing_method,
)
from orchestrator.nodes.qa_testing_strategist import (
    DEFAULT_QA_SIZING_METHOD,
    QA_SIZING_METHODS,
)

__all__ = [
    "QaSizingResponse",
    "QaSizingUpdate",
    "get_qa_sizing_method",
    "update_qa_sizing_method",
]

_SETTING_KEY = "qa_sizing_method"


async def get_qa_sizing_method() -> QaSizingResponse:
    return await get_sizing_method(_SETTING_KEY, DEFAULT_QA_SIZING_METHOD, QA_SIZING_METHODS)


async def update_qa_sizing_method(update: QaSizingUpdate) -> QaSizingResponse:
    return await update_sizing_method(
        _SETTING_KEY, DEFAULT_QA_SIZING_METHOD, QA_SIZING_METHODS, update
    )

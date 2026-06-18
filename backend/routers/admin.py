"""Admin endpoints backing the Settings screen.

Read and update the global tunables — AI-reduction guardrail bands, team-scaling
coefficients, the default rate card, the Development/QA sizing methods, and the
contingency reserve — each merging code defaults with Postgres overrides. When
Postgres is disabled an update is not persisted and the response's ``editable`` flag
is false so the UI can warn the change wasn't saved.

Every setting exposes the same GET (read effective state) / PUT (validate + persist +
re-read) pair, so the routes are registered from one table rather than hand-written per
setting. Each handler's real logic lives in its ``*_admin`` module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from fastapi import APIRouter
from pydantic import BaseModel

from contingency_admin import (
    ContingencyResponse,
    get_contingency,
    update_contingency,
)
from dev_sizing_admin import (
    DevSizingResponse,
    get_dev_sizing_method,
    update_dev_sizing_method,
)
from discovery_sizing_admin import (
    DiscoverySizingResponse,
    get_discovery_sizing_method,
    update_discovery_sizing_method,
)
from qa_sizing_admin import (
    QaSizingResponse,
    get_qa_sizing_method,
    update_qa_sizing_method,
)
from rate_card_admin import (
    RateCardResponse,
    get_effective_rates,
    update_rates,
)
from reduction_bands_admin import (
    ReductionBandsResponse,
    get_effective_bands,
    update_bands,
)
from staffing_admin import (
    StaffingCoefficientsResponse,
    get_effective_staffing,
    update_staffing,
)

router = APIRouter(prefix="/admin", tags=["admin"])


class _AdminSetting(NamedTuple):
    """One Settings tunable: its path, response model, and GET/PUT handlers. The PUT
    handler's request-body type is inferred by FastAPI from the ``update_fn`` signature."""

    path: str
    response_model: type[BaseModel]
    read_fn: Callable[[], Awaitable[Any]]
    update_fn: Callable[..., Awaitable[Any]]
    summary: str


_ADMIN_SETTINGS: tuple[_AdminSetting, ...] = (
    _AdminSetting(
        "/reduction-bands", ReductionBandsResponse, get_effective_bands, update_bands,
        "AI-reduction guardrail bands (code defaults merged with DB overrides)",
    ),
    _AdminSetting(
        "/staffing-coefficients", StaffingCoefficientsResponse,
        get_effective_staffing, update_staffing,
        "Team-scaling (Brooks's-Law / diminishing-returns) coefficients",
    ),
    _AdminSetting(
        "/default-rates", RateCardResponse, get_effective_rates, update_rates,
        "Default rate card (hourly rate per role category × seniority)",
    ),
    _AdminSetting(
        "/discovery-sizing-method", DiscoverySizingResponse,
        get_discovery_sizing_method, update_discovery_sizing_method,
        "Discovery sizing method (Use Case Points default, or FP-based analysis effort)",
    ),
    _AdminSetting(
        "/development-sizing-method", DevSizingResponse,
        get_dev_sizing_method, update_dev_sizing_method,
        "Development sizing method (COCOMO II default, or Function Points)",
    ),
    _AdminSetting(
        "/qa-sizing-method", QaSizingResponse,
        get_qa_sizing_method, update_qa_sizing_method,
        "QA/testing sizing method (TPA default, or Test Case Point Analysis)",
    ),
    _AdminSetting(
        "/contingency", ContingencyResponse, get_contingency, update_contingency,
        "Global contingency management-reserve % (uplifts final cost + timeline)",
    ),
)

# When Postgres is disabled the PUT does not persist; the response's `editable` is false.
_PUT_NOTE = " Persists the change and returns the new effective state; no-ops (editable=false) when Postgres is disabled."

for _s in _ADMIN_SETTINGS:
    router.add_api_route(
        _s.path, _s.read_fn, methods=["GET"],
        response_model=_s.response_model, summary=f"Read the {_s.summary}.",
    )
    router.add_api_route(
        _s.path, _s.update_fn, methods=["PUT"],
        response_model=_s.response_model, summary=f"Update the {_s.summary}.{_PUT_NOTE}",
    )

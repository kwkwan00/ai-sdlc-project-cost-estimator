"""Admin surface for the rate card.

Two parts, edited together on one Settings screen and persisted by ``PUT /admin/default-rates``:

1. The fixed **grid** — an hourly rate per role ``category × seniority`` (merges the in-code
   ``pricing.DEFAULT_RATES`` with any Postgres overrides; the 28 cells can be edited but not
   added/removed).
2. **Custom roles** — admin-defined named roles (label + category + seniority + rate) that the
   admin can add/delete/edit on top of the grid. Roster editors offer them as a catalog (read via
   ``GET /role-catalog`` — see ``routers/catalog.py``) to prefill roster rows.

Writes degrade gracefully: when Postgres is disabled the GET still returns the code defaults (with
no custom roles) read-only, and the PUT reports it wasn't persisted (``editable=false``). Mirrors
``staffing_admin.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from config import get_settings
from db.repositories import (
    CustomRoleRecord,
    get_custom_roles,
    get_default_rates,
    replace_rate_card,
)
from models.twin_outputs import RoleCategory, RoleSeniority
from pricing import DEFAULT_RATES, RATE_BOUNDS
from slug import slugify, unique_slug

logger = logging.getLogger(__name__)

# --- grid (category × seniority) ------------------------------------------------------------


class RateRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    seniority: str
    rate: float
    default_rate: float
    is_override: bool


class RateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    seniority: str
    rate: float


# --- custom roles (named, admin-managed) ----------------------------------------------------


class CustomRoleRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_id: str
    label: str
    category: str
    seniority: str
    rate: float


class CustomRoleInputRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Omitted/blank on a freshly-added row → the server assigns a slug id from the label.
    role_id: str | None = None
    label: str
    category: str
    seniority: str
    rate: float


# --- responses / requests -------------------------------------------------------------------


class RateCardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # True when Postgres is connected and edits will persist.
    editable: bool
    min_rate: float
    max_rate: float
    rates: list[RateRow]
    custom_roles: list[CustomRoleRow] = Field(default_factory=list)


class RateCardUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rates: list[RateInput]
    # The complete desired set of custom roles — the backend makes the table match it exactly (rows
    # absent from the list are deleted). Three-state on purpose: omission (None) leaves the existing
    # custom roles untouched; an explicit list — even an empty one — REPLACES the set (so `[]`
    # deletes all). See update_rates.
    custom_roles: list[CustomRoleInputRow] | None = None


class RoleCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[CustomRoleRow]


def _record_to_row(r: CustomRoleRecord) -> CustomRoleRow:
    return CustomRoleRow(
        role_id=r.role_id, label=r.label, category=r.category, seniority=r.seniority, rate=r.rate
    )


async def get_effective_rates() -> RateCardResponse:
    """Merge code defaults with DB overrides into editable grid rows (all 28 cells), plus the
    admin-defined custom roles."""
    # Two independent reads of disjoint tables — run concurrently.
    overrides, custom = await asyncio.gather(get_default_rates(), get_custom_roles())
    rows = [
        RateRow(
            category=cat.value,
            seniority=sen.value,
            rate=overrides.get((cat, sen), default),
            default_rate=default,
            is_override=(cat, sen) in overrides,
        )
        for (cat, sen), default in DEFAULT_RATES.items()
    ]
    lo, hi = RATE_BOUNDS
    return RateCardResponse(
        editable=get_settings().postgres_enabled,
        min_rate=lo,
        max_rate=hi,
        rates=rows,
        custom_roles=[_record_to_row(r) for r in custom],
    )


@dataclass
class _ParsedRole:
    """One validated custom-role row mid-flight; ``role_id`` is filled during id resolution."""

    label: str
    category: RoleCategory
    seniority: RoleSeniority
    rate: float
    supplied_id: str | None
    role_id: str = ""


def _validate_custom_roles(
    inputs: list[CustomRoleInputRow], *, lo: float, hi: float
) -> list[CustomRoleRecord]:
    """Validate the desired custom-role set + resolve each row's ``role_id``.

    Identity is **stable**: rows carrying an explicit ``role_id`` (existing roles round-tripped from
    a prior GET) keep that id **verbatim** — they are reserved FIRST and never re-slugified, so a
    non-canonical stored id can't be silently re-keyed into a delete+recreate. Only brand-new rows
    (no role_id) get a freshly minted slug, suffixed to avoid any reserved/used id. A duplicate
    supplied id is a malformed request and is rejected (it would otherwise split into two roles)."""
    # Validate every row up front (a 422 must reject the whole request before any id is assigned).
    parsed: list[_ParsedRole] = []
    for cr in inputs:
        label = cr.label.strip()
        if not label:
            raise HTTPException(422, "Custom role label must not be empty")
        try:
            cat, sen = RoleCategory(cr.category), RoleSeniority(cr.seniority)
        except ValueError as exc:
            raise HTTPException(
                422, f"Unknown role tags {cr.category!r}/{cr.seniority!r} for {label!r}"
            ) from exc
        if not lo <= cr.rate <= hi:
            raise HTTPException(422, f"rate for {label!r} must be in [{lo}, {hi}] (got {cr.rate})")
        parsed.append(_ParsedRole(label, cat, sen, cr.rate, cr.role_id or None))

    used_ids: set[str] = set()
    # Pass 1 — reserve existing roles' ids VERBATIM (server-minted, already canonical). Keeping the
    # exact id is what makes a round-tripped edit land on the same DB row; a duplicate supplied id
    # is rejected rather than re-keyed.
    for p in parsed:
        if p.supplied_id:
            if p.supplied_id in used_ids:
                raise HTTPException(422, f"Duplicate custom role id {p.supplied_id!r}")
            used_ids.add(p.supplied_id)
            p.role_id = p.supplied_id
    # Pass 2 — mint ids for brand-new rows AROUND the reserved set.
    for p in parsed:
        if not p.supplied_id:
            p.role_id = unique_slug(slugify(p.label), used_ids)
    return [
        CustomRoleRecord(role_id=p.role_id, label=p.label, category=p.category.value,
                         seniority=p.seniority.value, rate=p.rate)
        for p in parsed
    ]


async def update_rates(update: RateCardUpdate) -> RateCardResponse:
    """Validate + persist edited grid rates and the custom-role set, then return the new state.

    ``custom_roles=None`` (omitted) leaves the existing custom roles untouched; an explicit list
    (even empty) **replaces** the set — so deleting every custom role is sending ``[]``."""
    lo, hi = RATE_BOUNDS
    # Validate EVERYTHING before writing anything — a 422 must leave the rate card untouched.
    items: list[tuple[RoleCategory, RoleSeniority, float]] = []
    for r in update.rates:
        try:
            cat, sen = RoleCategory(r.category), RoleSeniority(r.seniority)
        except ValueError as exc:
            raise HTTPException(422, f"Unknown role cell {r.category!r}/{r.seniority!r}") from exc
        if not lo <= r.rate <= hi:
            raise HTTPException(422, f"rate for {cat.value}/{sen.value} must be in [{lo}, {hi}] (got {r.rate})")
        items.append((cat, sen, r.rate))
    records = (
        _validate_custom_roles(update.custom_roles, lo=lo, hi=hi)
        if update.custom_roles is not None
        else None
    )

    # Persist the grid + custom roles ATOMICALLY (one transaction) so a PUT can't half-apply.
    persisted = await replace_rate_card(items, records)
    if not persisted:
        logger.warning("rate-card update not persisted (Postgres disabled/failed)")
    # Re-read rather than echo the in-memory inputs: get_effective_rates returns the AUTHORITATIVE
    # stored state, which is what the client must see when persistence is disabled/failed (the edits
    # did NOT land — `editable=false`, code defaults) or when custom_roles was omitted (untouched,
    # so unknown here). Two small extra reads on an infrequent admin save is the right trade.
    return await get_effective_rates()


async def get_role_catalog() -> RoleCatalogResponse:
    """The admin-defined custom roles, for the Stage 2 roster editor's 'add from catalog' picker.
    ``{roles: []}`` when Postgres is disabled / none are defined."""
    return RoleCatalogResponse(roles=[_record_to_row(r) for r in await get_custom_roles()])

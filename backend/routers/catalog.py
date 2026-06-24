"""Org-level role catalog (the admin-defined custom rate-card roles), read-only.

Separate from both ``/admin`` (which owns editing) and ``/estimates/draft`` (wizard-scoped): the
catalog is org-level reference data consumed wherever a roster is built — the Stage 2 wizard AND
the WBS team page — so it gets its own unprefixed, auth-neutral home rather than riding on the
draft namespace.
"""

from __future__ import annotations

from fastapi import APIRouter

from admin.rate_card_admin import RoleCatalogResponse, get_role_catalog

router = APIRouter(tags=["catalog"])


@router.get("/role-catalog", response_model=RoleCatalogResponse)
async def role_catalog() -> RoleCatalogResponse:
    """The admin-defined custom rate-card roles, offered as a catalog in roster editors so users can
    insert a prefilled role (label + category + seniority + rate) in one click. Returns
    ``{roles: []}`` when Postgres is off / none are defined — editors just hide the picker then."""
    return await get_role_catalog()

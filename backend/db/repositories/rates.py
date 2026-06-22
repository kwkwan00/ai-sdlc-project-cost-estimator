"""Default rate card — a ``(category, seniority) → rate`` grid plus admin-defined custom roles,
read by the Settings admin screen and the roster agent. Mirrors ``db/repositories/staffing.py``.

``get_default_rates()`` returns the DB grid overrides as ``{(RoleCategory, RoleSeniority): rate}``;
callers merge them over ``pricing.DEFAULT_RATES`` — the code default fills any missing cell.
``get_custom_roles()`` returns the named custom roles. ``replace_rate_card(...)`` persists an edit
to BOTH (grid upsert + custom-role set-replace) in ONE transaction so a save can't half-apply.

All honor the never-raise persistence contract (via the ``_common`` helpers / ``session_scope``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.orm_models import CustomRateRole, DefaultRate
from db.postgres_adapter import session_scope
from db.repositories._common import fetch_all_rows
from models.twin_outputs import RoleCategory, RoleSeniority

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CustomRoleRecord:
    """One admin-defined custom rate-card role (named, on top of the fixed grid). The
    category/seniority are kept as raw strings here — the admin layer validates them against the
    enums; the repo stays storage-only."""

    role_id: str
    label: str
    category: str
    seniority: str
    rate: float


async def get_default_rates() -> dict[tuple[RoleCategory, RoleSeniority], float]:
    """DB-stored rate-card overrides as ``{(category, seniority): rate}``. Empty when Postgres is
    disabled / the table is empty / unreadable; a row with an unrecognized enum value is skipped.
    Callers fall back to ``pricing.DEFAULT_RATES`` per cell."""
    out: dict[tuple[RoleCategory, RoleSeniority], float] = {}
    for r in await fetch_all_rows(DefaultRate):
        try:
            out[(RoleCategory(r.category), RoleSeniority(r.seniority))] = r.rate
        except ValueError:
            logger.warning("skipping unrecognized rate-card row %s/%s", r.category, r.seniority)
    return out




# --- custom rate-card roles (admin-managed named roles on top of the grid) -----------------


async def get_custom_roles() -> list[CustomRoleRecord]:
    """All admin-defined custom rate-card roles, sorted by label for a stable display order.
    ``[]`` when Postgres is disabled / the table is empty / unreadable. A row whose stored
    category/seniority is no longer a valid enum value (enum rename, hand-edited row) is skipped —
    mirroring ``get_default_rates`` — so callers/the frontend never receive an out-of-enum tag."""
    records: list[CustomRoleRecord] = []
    for r in await fetch_all_rows(CustomRateRole):
        try:
            RoleCategory(r.category), RoleSeniority(r.seniority)
        except ValueError:
            logger.warning("skipping custom role %s with unrecognized tags %s/%s",
                           r.role_id, r.category, r.seniority)
            continue
        records.append(
            CustomRoleRecord(
                role_id=r.role_id,
                label=r.label,
                category=r.category,
                seniority=r.seniority,
                rate=r.rate,
            )
        )
    return sorted(records, key=lambda r: (r.label.lower(), r.role_id))


def _apply_custom_role(row: CustomRateRole, item: CustomRoleRecord) -> None:
    row.label = item.label
    row.category = item.category
    row.seniority = item.seniority
    row.rate = item.rate


def _make_custom_role(item: CustomRoleRecord) -> CustomRateRole:
    return CustomRateRole(
        role_id=item.role_id,
        label=item.label,
        category=item.category,
        seniority=item.seniority,
        rate=item.rate,
    )


async def replace_rate_card(
    grid: list[tuple[RoleCategory, RoleSeniority, float]],
    custom: list[CustomRoleRecord] | None,
) -> bool:
    """Apply a full rate-card edit **atomically** in ONE transaction: upsert the grid overrides AND
    (when ``custom`` is not None) make ``custom_rate_roles`` EXACTLY match ``custom`` (add new, update
    existing, delete any role_id no longer present). Either both writes land or neither — a PUT can
    never half-apply (grid saved while custom roles fail, or vice-versa). ``custom=None`` leaves the
    custom roles untouched. Returns ``True`` when persisted, ``False`` when Postgres is disabled or
    the write fails.

    The ``try`` wraps the whole ``async with session_scope()`` block — ``session_scope`` owns the
    rollback (the never-raise / asyncpg contract; see ``_common.upsert_keyed``). Don't move it."""
    try:
        async with session_scope() as session:
            if session is None:
                return False
            # Grid: additive upsert (the 28 cells are fixed; rows are only ever updated/inserted).
            grid_rows = {
                (r.category, r.seniority): r
                for r in (await session.execute(select(DefaultRate))).scalars().all()
            }
            for cat, sen, rate in grid:
                row = grid_rows.get((cat.value, sen.value))
                if row is not None:
                    row.rate = rate
                else:
                    session.add(DefaultRate(category=cat.value, seniority=sen.value, rate=rate))
            # Custom roles: explicit set-replace (upsert + delete orphans) when provided.
            if custom is not None:
                custom_rows = {
                    r.role_id: r
                    for r in (await session.execute(select(CustomRateRole))).scalars().all()
                }
                wanted = {rec.role_id for rec in custom}
                for rec in custom:
                    crow = custom_rows.get(rec.role_id)
                    if crow is not None:
                        _apply_custom_role(crow, rec)
                    else:
                        session.add(_make_custom_role(rec))
                for rid, crow in custom_rows.items():
                    if rid not in wanted:
                        await session.delete(crow)
        return True
    except asyncio.CancelledError:
        raise
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("replace_rate_card failed (%s)", exc)
        return False

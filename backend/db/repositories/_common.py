"""Shared helpers for the repositories package.

Only the pieces used by more than one repository module live here (currently the
codebase-context → integer code mapping used by both history persistence and the
calibration read path, plus the generic keyed key-value config-table read/upsert
helpers shared by the bands and staffing modules) so the concern-split modules
don't import from each other.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.postgres_adapter import session_scope
from models.project_schema import CodebaseContext, Stage3Context

logger = logging.getLogger(__name__)


def codebase_code(stage3: Stage3Context | None) -> int | None:
    """Map the project-level codebase context to its integer code (0–3).

    Phase-independent: the codebase context is a single project-level signal, not a
    per-phase one. The returned code is stored in the column historically named
    `maturity_level` (no migration — the column was repurposed). Returns None when
    Stage 3 is absent.
    """
    if stage3 is None:
        return None
    mapping = {
        CodebaseContext.GREENFIELD: 0,
        CodebaseContext.BROWNFIELD_SMALL: 1,
        CodebaseContext.BROWNFIELD_LARGE_UNFAMILIAR: 2,
        CodebaseContext.BROWNFIELD_LARGE_FAMILIAR: 3,
    }
    return mapping.get(stage3.codebase_context)


async def fetch_all_rows[Model](model: type[Model]) -> list[Model]:
    """Read every row of a small config table under the never-raise contract.

    Returns ``[]`` when Postgres is disabled or the read fails. The try/except lives
    **inside** the ``async with session_scope()`` block and ``await session.rollback()``
    runs before returning the empty case — on asyncpg a poisoned transaction would
    otherwise re-raise out of ``session_scope``'s clean-exit commit. Do not move the
    rollback or the try boundary.
    """
    rows: list[Model] = []
    async with session_scope() as session:
        if session is None:
            logger.debug("postgres disabled; no %s rows (using code defaults)", model.__name__)
            return []
        try:
            result = await session.execute(select(model))
            rows = list(result.scalars().all())
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError) as exc:
            await session.rollback()
            logger.warning("fetch_all_rows(%s) failed (%s); using code defaults", model.__name__, exc)
            return []
    return rows


async def upsert_keyed[Model, Item, Key](
    model: type[Model],
    items: list[Item],
    *,
    key_of: Callable[[Item], Key],
    key_of_row: Callable[[Model], Key],
    apply: Callable[[Model, Item], None],
    make: Callable[[Item], Model],
) -> bool:
    """Snapshot + update-or-insert a small keyed config table under the never-raise contract.

    Snapshots existing rows into ``{key_of_row(row): row}``, then for each item either applies it
    onto the matching row (``apply``) or inserts a new one (``make``). Purely **additive** — it never
    deletes; a key absent from ``items`` is left untouched. (For a destructive set-replace, write a
    dedicated transaction so the deletion is explicit at the call site — see
    ``rates.replace_rate_card`` — rather than hiding it behind a flag here.) Returns ``True`` when
    persisted, ``False`` when Postgres is disabled or the write fails.

    The ``try`` wraps the **whole** ``async with session_scope()`` block (matching the
    original upsert structure): on error ``session_scope``'s own exit handling rolls back,
    so there is no in-block rollback here. Do not change WHEN rollback happens — moving the
    try inside the ``async with`` would break the asyncpg never-raise guarantee.
    """
    try:
        async with session_scope() as session:
            if session is None:
                return False
            existing: dict[Key, Model] = {
                key_of_row(r): r
                for r in (await session.execute(select(model))).scalars().all()
            }
            for item in items:
                row = existing.get(key_of(item))
                if row is not None:
                    apply(row, item)
                else:
                    session.add(make(item))
        return True
    except asyncio.CancelledError:
        raise
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("upsert_keyed(%s) failed (%s)", model.__name__, exc)
        return False

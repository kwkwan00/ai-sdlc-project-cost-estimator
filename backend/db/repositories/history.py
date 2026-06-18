"""Estimate-history persistence: denormalize an `EstimateEnvelope` into the
history tables and read it back for the dashboard / redisplay.

`save_estimate_history(envelope, ...)` is idempotent on `estimate_id` — calling it
repeatedly during a run upserts the same row (Pass 2 supersedes Pass 1 in-place).
Mirrors Neo4j's `save_estimate_envelope` semantics: silently no-ops when Postgres is
disabled. The companion readers (`list_estimate_history`, `get_estimate_envelope`)
return the empty case when Postgres is disabled or unreadable.

Every function honors the never-raise persistence contract: DB errors are caught,
logged, and converted to the empty case so the HTTP layer never fails because of
persistence.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from db.orm_models import EstimateHistory, PhaseHistory
from db.postgres_adapter import session_scope
from db.repositories._common import codebase_code
from models.project_schema import EstimateEnvelope, Stage2Context, Stage3Context
from models.twin_outputs import DualScenarioEstimate, PhaseEstimate

logger = logging.getLogger(__name__)


def _phase_row(
    estimate_id: str,
    p: PhaseEstimate,
    *,
    industry: str | None,
    project_type: str | None,
    maturity: int | None,
) -> dict[str, Any]:
    return {
        "estimate_id": estimate_id,
        "phase": p.phase.value,
        "twin_name": p.twin_name,
        "algorithm": p.algorithm,
        "ai_assisted_optimistic": p.ai_assisted_hours.optimistic,
        "ai_assisted_mid": p.ai_assisted_hours.most_likely,
        "ai_assisted_pessimistic": p.ai_assisted_hours.pessimistic,
        "manual_only_optimistic": p.manual_only_hours.optimistic,
        "manual_only_mid": p.manual_only_hours.most_likely,
        "manual_only_pessimistic": p.manual_only_hours.pessimistic,
        "confidence": p.confidence,
        "maturity_level": maturity,
        "industry": industry,
        "project_type": project_type,
    }


def _apply_final_estimate(row: EstimateHistory, final: DualScenarioEstimate) -> None:
    """Copy the rolled-up final-estimate fields onto the history row."""
    row.total_ai_assisted_mid_hours = final.total_ai_assisted_hours.most_likely
    row.total_manual_only_mid_hours = final.total_manual_only_hours.most_likely
    row.ai_hours_saved = final.ai_hours_saved_pert
    row.ai_cost_saved_usd = final.ai_cost_saved_usd
    row.total_cost_ai_assisted_usd = final.total_cost_ai_assisted_usd
    row.total_cost_manual_only_usd = final.total_cost_manual_only_usd
    row.confidence = final.confidence
    row.duration_weeks_low = final.duration_weeks_low
    row.duration_weeks_high = final.duration_weeks_high


async def _replace_phase_rows(
    session: Any,
    envelope: EstimateEnvelope,
    *,
    industry: str | None,
    project_type: str | None,
    stage3: Stage3Context | None,
) -> None:
    """Replace an estimate's phase rows wholesale — delete then re-insert rather than
    diffing individual rows, so Pass 2 supersedes Pass 1 cleanly. Logs whether any
    rows were written."""
    phases = envelope.pass2_estimates or envelope.pass1_estimates
    if not phases:
        logger.info(
            "postgres: persisted history for estimate %s (no phase rows written)",
            envelope.estimate_id,
        )
        return
    await session.execute(
        delete(PhaseHistory).where(PhaseHistory.estimate_id == envelope.estimate_id)
    )
    rows = [
        _phase_row(
            envelope.estimate_id,
            p,
            industry=industry,
            project_type=project_type,
            maturity=codebase_code(stage3),
        )
        for p in phases
    ]
    session.add_all([PhaseHistory(**r) for r in rows])
    logger.info(
        "postgres: persisted history for estimate %s (%d phase row(s))",
        envelope.estimate_id,
        len(phases),
    )


async def save_estimate_history(
    envelope: EstimateEnvelope,
    *,
    stage2: Stage2Context | None,
    stage3: Stage3Context | None,
) -> None:
    """Upsert one envelope + its phases into the history tables.

    Silently no-ops when Postgres is disabled or unreachable.
    """
    async with session_scope() as session:
        if session is None:
            logger.debug(
                "postgres disabled; skipping history persistence for estimate %s",
                envelope.estimate_id,
            )
            return
        try:
            industry = stage2.industry if stage2 else None
            project_type = stage2.project_type.value if stage2 else None
            engagement = stage2.engagement_model.value if stage2 else None
            target_weeks = stage2.target_timeline_weeks if stage2 else None
            final: DualScenarioEstimate | None = envelope.final_estimate

            existing = await session.get(EstimateHistory, envelope.estimate_id)
            if existing is None:
                existing = EstimateHistory(id=envelope.estimate_id)
                session.add(existing)

            existing.project_name = envelope.project_name
            existing.status = envelope.status.value
            existing.envelope_json = envelope.model_dump(mode="json")
            # Stage2-derived metadata is only written when stage2 is actually provided.
            # On a Pass-2 FAILURE the caller passes stage2=None; nulling these would
            # wipe the values Pass 1 already populated (data-loss bug). When stage2 is
            # absent, preserve whatever is already stored on the row.
            if stage2 is not None:
                existing.industry = industry
                existing.project_type = project_type
                existing.engagement_model = engagement
                existing.target_timeline_weeks = target_weeks
            if final is not None:
                _apply_final_estimate(existing, final)

            await _replace_phase_rows(
                session,
                envelope,
                industry=industry,
                project_type=project_type,
                stage3=stage3,
            )
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError) as exc:
            # Roll back so session_scope's clean-exit commit is a no-op: otherwise a
            # mid-body failure would leave a partial write to commit, and on asyncpg the
            # poisoned transaction would make that commit re-raise out of this function
            # (breaking the never-raise contract). Scoped to DB/connection errors so
            # programmer bugs (AttributeError/KeyError) still surface in tests.
            await session.rollback()
            logger.warning("save_estimate_history failed (%s); skipping", exc)
            return


async def list_estimate_history(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Recent persisted estimates (newest first) as summary dicts for the history
    list, sliced by `limit`/`offset` for paging. Returns [] when Postgres is disabled
    or unreadable."""
    async with session_scope() as session:
        if session is None:
            return []
        try:
            result = await session.execute(
                select(EstimateHistory)
                .order_by(EstimateHistory.updated_at.desc())
                .offset(offset)
                .limit(limit)
            )
            rows = list(result.scalars().all())
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError) as exc:
            await session.rollback()
            logger.warning("list_estimate_history failed (%s)", exc)
            return []
    return [
        {
            "estimate_id": r.id,
            "project_name": r.project_name,
            "status": r.status,
            "industry": r.industry,
            "project_type": r.project_type,
            "total_ai_assisted_hours": r.total_ai_assisted_mid_hours,
            "total_manual_only_hours": r.total_manual_only_mid_hours,
            "ai_hours_saved": r.ai_hours_saved,
            "total_cost_ai_assisted_usd": r.total_cost_ai_assisted_usd,
            "confidence": r.confidence,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


async def count_estimate_history() -> int:
    """Total number of persisted estimates — the denominator for the dashboard's
    page controls. Returns 0 when Postgres is disabled or unreadable."""
    async with session_scope() as session:
        if session is None:
            return 0
        try:
            result = await session.execute(
                select(func.count()).select_from(EstimateHistory)
            )
            return int(result.scalar_one())
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError) as exc:
            await session.rollback()
            logger.warning("count_estimate_history failed (%s)", exc)
            return 0


async def delete_estimate_history(estimate_id: str) -> bool:
    """Delete an estimate's history row and its phase rows. Returns True when a row
    was removed, False when the id wasn't present or Postgres is disabled/unreadable.
    Phase rows are deleted explicitly (mirroring `save_estimate_history`) so it works
    even where the FK cascade isn't enforced (e.g. SQLite in tests)."""
    async with session_scope() as session:
        if session is None:
            return False
        try:
            if await session.get(EstimateHistory, estimate_id) is None:
                return False
            await session.execute(
                delete(PhaseHistory).where(PhaseHistory.estimate_id == estimate_id)
            )
            await session.execute(
                delete(EstimateHistory).where(EstimateHistory.id == estimate_id)
            )
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError) as exc:
            await session.rollback()
            logger.warning("delete_estimate_history failed (%s)", exc)
            return False
    return True


async def get_estimate_envelope(estimate_id: str) -> dict[str, Any] | None:
    """The full persisted EstimateEnvelope JSON for redisplay, or None when Postgres
    is disabled / the estimate isn't in history / no snapshot was stored."""
    async with session_scope() as session:
        if session is None:
            return None
        try:
            row = await session.get(EstimateHistory, estimate_id)
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError) as exc:
            await session.rollback()
            logger.warning("get_estimate_envelope failed (%s)", exc)
            return None
        return row.envelope_json if row else None

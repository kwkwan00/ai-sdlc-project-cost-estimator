"""Twin calibration aggregates: recompute rolling per-(phase, industry,
project_type, codebase-context) averages from `phase_history`, and read them back
for the twins.

`refresh_calibration_for_phase(phase)` recomputes one phase's aggregates after
estimates complete so subsequent runs see the new data. `get_calibration(...)` reads
aggregates back as plain dicts (no ORM leakage) so callers (twins / parse_input) can
hand them straight to the LLM prompt. The codebase-context code rides in the column
historically named `maturity_level`.

Every function honors the never-raise persistence contract: DB errors are caught,
logged, and converted to the empty case (0 rows / []).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from db.orm_models import CalibrationAggregate, PhaseHistory
from db.postgres_adapter import session_scope
from db.repositories._common import codebase_code
from models.project_schema import Stage3Context

logger = logging.getLogger(__name__)


_ANY = ""  # sentinel for "any value" in CalibrationAggregate string dimensions
# The `maturity_level` column now holds a codebase-context code (0–3: greenfield,
# brownfield_small, brownfield_large_unfamiliar, brownfield_large_familiar). 0 is a
# real, common (default) code and can't double as the "any" sentinel, so use -1
# (outside 0–3) instead. The column keeps its historical name to avoid a migration.
_ANY_MATURITY = -1


async def refresh_calibration_for_phase(phase_value: str) -> int:
    """Recompute calibration aggregates for one phase from phase_history.

    Returns the number of aggregate rows written. Skips silently when Postgres is
    disabled. Aggregates are computed for **every** (industry, project_type,
    codebase-context) triple present in phase_history, plus a single "any" rollup with
    industry="" / project_type="" / maturity_level=-1 that twins use as a fallback.
    The codebase-context code lives in the column historically named `maturity_level`.
    """
    async with session_scope() as session:
        if session is None:
            logger.debug(
                "postgres disabled; skipping calibration refresh for phase %s", phase_value
            )
            return 0
        try:
            return await _refresh_calibration(session, phase_value)
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError) as exc:
            # _refresh_calibration deletes then re-inserts aggregates; roll back so a
            # failed insert can't leave the delete committed (wiping a phase's
            # aggregates with no replacement) and so the trailing commit can't re-raise.
            # Scoped to DB/connection errors so programmer bugs still surface in tests.
            await session.rollback()
            logger.warning(
                "refresh_calibration_for_phase failed for phase %s (%s); skipping",
                phase_value,
                exc,
            )
            return 0


async def _refresh_calibration(session: Any, phase_value: str) -> int:
    """Inner body of refresh_calibration_for_phase (caller guards + handles errors)."""
    # Per-dimension aggregates.
    per_dim_q = await session.execute(
        select(
            PhaseHistory.industry,
            PhaseHistory.project_type,
            PhaseHistory.maturity_level,
            _avg(PhaseHistory.ai_assisted_mid).label("avg_ai"),
            _avg(PhaseHistory.manual_only_mid).label("avg_manual"),
            _avg(PhaseHistory.confidence).label("avg_conf"),
            _count(PhaseHistory.id).label("n"),
        )
        .where(PhaseHistory.phase == phase_value)
        .group_by(
            PhaseHistory.industry, PhaseHistory.project_type, PhaseHistory.maturity_level
        )
    )
    rows = per_dim_q.all()

    # "Any" rollup across the whole phase.
    any_q = await session.execute(
        select(
            _avg(PhaseHistory.ai_assisted_mid).label("avg_ai"),
            _avg(PhaseHistory.manual_only_mid).label("avg_manual"),
            _avg(PhaseHistory.confidence).label("avg_conf"),
            _count(PhaseHistory.id).label("n"),
        ).where(PhaseHistory.phase == phase_value)
    )
    any_row = any_q.one()

    # Clear stale rows for this phase, then bulk insert.
    await session.execute(
        delete(CalibrationAggregate).where(CalibrationAggregate.phase == phase_value)
    )

    written = 0
    if any_row.n and any_row.n > 0:
        session.add(
            CalibrationAggregate(
                phase=phase_value,
                industry=_ANY,
                project_type=_ANY,
                maturity_level=_ANY_MATURITY,
                sample_count=int(any_row.n),
                avg_ai_assisted_mid=float(any_row.avg_ai or 0.0),
                avg_manual_only_mid=float(any_row.avg_manual or 0.0),
                avg_confidence=float(any_row.avg_conf or 0.0),
                avg_ai_reduction_pct=_reduction_pct(any_row.avg_manual, any_row.avg_ai),
            )
        )
        written += 1

    for r in rows:
        session.add(
            CalibrationAggregate(
                phase=phase_value,
                industry=(r.industry or _ANY),
                project_type=(r.project_type or _ANY),
                maturity_level=(
                    r.maturity_level if r.maturity_level is not None else _ANY_MATURITY
                ),
                sample_count=int(r.n),
                avg_ai_assisted_mid=float(r.avg_ai or 0.0),
                avg_manual_only_mid=float(r.avg_manual or 0.0),
                avg_confidence=float(r.avg_conf or 0.0),
                avg_ai_reduction_pct=_reduction_pct(r.avg_manual, r.avg_ai),
            )
        )
        written += 1

    logger.info(
        "postgres: refreshed calibration for phase %s (%d aggregate row(s))",
        phase_value,
        written,
    )
    return written


async def get_calibration(
    phase_value: str,
    *,
    industry: str | None = None,
    project_type: str | None = None,
    maturity: int | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return calibration rows for a phase, most-specific first.

    Ranking is by how many dimensions match the request (exact > partial > "any"),
    then by sample_count. Useful for twin prompts: hand the LLM the most relevant
    aggregate first, fall back to broader ones.

    Returns [] when Postgres is disabled — callers must handle the empty case
    (it's the same as "no calibration data yet", which is the cold-start reality).

    The `maturity` param / `maturity_level` field carry the codebase-context code
    (0–3; -1 = any), keeping their historical names to avoid a migration.
    """
    async with session_scope() as session:
        if session is None:
            logger.debug(
                "postgres disabled; returning no calibration for phase %s", phase_value
            )
            return []
        try:
            result = await session.execute(
                select(CalibrationAggregate).where(CalibrationAggregate.phase == phase_value)
            )
            rows = result.scalars().all()
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError) as exc:
            await session.rollback()
            logger.warning(
                "get_calibration failed for phase %s (%s); returning none",
                phase_value,
                exc,
            )
            return []
        if not rows:
            logger.debug("no calibration aggregates stored for phase %s", phase_value)
            return []

        ind = (industry or "").strip()
        ptype = (project_type or "").strip()
        mat = maturity if maturity is not None else _ANY_MATURITY

        def score(r: CalibrationAggregate) -> tuple[int, int]:
            specificity = (
                int(r.industry == ind and ind != "")
                + int(r.project_type == ptype and ptype != "")
                + int(r.maturity_level == mat and mat != _ANY_MATURITY)
            )
            return (specificity, r.sample_count)

        ranked = sorted(rows, key=score, reverse=True)[:limit]
        logger.debug(
            "calibration lookup for phase %s returned %d row(s)", phase_value, len(ranked)
        )
        return [
            {
                "phase": r.phase,
                "industry": r.industry or None,
                "project_type": r.project_type or None,
                "maturity_level": (
                    r.maturity_level if r.maturity_level != _ANY_MATURITY else None
                ),
                "sample_count": r.sample_count,
                "avg_ai_assisted_mid": r.avg_ai_assisted_mid,
                "avg_manual_only_mid": r.avg_manual_only_mid,
                "avg_confidence": r.avg_confidence,
                "avg_ai_reduction_pct": r.avg_ai_reduction_pct,
            }
            for r in ranked
        ]


async def get_calibration_for_all_phases(
    *,
    industry: str | None = None,
    project_type: str | None = None,
    stage3: Stage3Context | None = None,
    per_phase_limit: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Convenience wrapper: fetch calibration for every SDLC phase at once.

    Called from `parse_input` to populate `state["calibration_examples"]` so the
    six twins can read their relevant slice from there during fan-out.
    """
    phases = (
        "discovery",
        "ux_design",
        "development",
        "code_review",
        "deployment",
        "qa_testing",
    )
    maturity = codebase_code(stage3) if stage3 else None
    # The per-phase queries are independent (each opens its own session), so run
    # them concurrently rather than serially.
    results = await asyncio.gather(
        *(
            get_calibration(
                ph,
                industry=industry,
                project_type=project_type,
                maturity=maturity,
                limit=per_phase_limit,
            )
            for ph in phases
        )
    )
    return dict(zip(phases, results, strict=True))


# ---------- private helpers ----------


def _avg(column):
    return func.avg(column)


def _count(column):
    return func.count(column)


def _reduction_pct(manual_mid: float | None, ai_mid: float | None) -> float:
    if not manual_mid or manual_mid <= 0:
        return 0.0
    ai_mid = ai_mid or 0.0
    if ai_mid >= manual_mid:
        return 0.0
    return float((manual_mid - ai_mid) / manual_mid) * 100.0

"""Repository functions for the Postgres persistence layer.

Three concerns, three small surfaces:

1. `save_estimate_history(envelope, ...)` — denormalize a completed (or in-progress)
   `EstimateEnvelope` into the history tables. Idempotent on `estimate_id` — calling
   it repeatedly during the run upserts the same row. Mirrors Neo4j's
   `save_estimate_envelope` semantics: silently no-ops when Postgres is disabled.

2. `refresh_calibration_for_phase(phase)` — recompute the rolling per-(phase,
   industry, project_type, codebase-context) aggregates from `phase_history`. Called
   after estimates complete so subsequent runs see the new data. The codebase-context
   code is stored in the column historically named `maturity_level`.

3. `get_calibration(phase, ...)` — read aggregates for a given phase, optionally
   filtered by industry / project_type / codebase-context. Returned as plain dicts so
   callers (twins / parse_input) can hand them straight to the LLM prompt without ORM
   leakage. The codebase-context code rides in the `maturity_level` column/param.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select

from db.orm_models import (
    AiReductionBand,
    CalibrationAggregate,
    EstimateHistory,
    PhaseHistory,
)
from db.postgres_adapter import session_scope
from models.project_schema import (
    CodebaseContext,
    EstimateEnvelope,
    Stage2Context,
    Stage3Context,
)
from models.twin_outputs import DualScenarioEstimate, PhaseEstimate

logger = logging.getLogger(__name__)


# ---------- helpers ----------


def _codebase_code(stage3: Stage3Context | None) -> int | None:
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


# ---------- save ----------


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
            existing.industry = industry
            existing.project_type = project_type
            existing.engagement_model = engagement
            existing.target_timeline_weeks = target_weeks
            if final is not None:
                existing.total_ai_assisted_mid_hours = final.total_ai_assisted_hours.most_likely
                existing.total_manual_only_mid_hours = final.total_manual_only_hours.most_likely
                existing.ai_hours_saved = final.ai_hours_saved_pert
                existing.ai_cost_saved_usd = final.ai_cost_saved_usd
                existing.total_cost_ai_assisted_usd = final.total_cost_ai_assisted_usd
                existing.total_cost_manual_only_usd = final.total_cost_manual_only_usd
                existing.confidence = final.confidence
                existing.duration_weeks_low = final.duration_weeks_low
                existing.duration_weeks_high = final.duration_weeks_high

            # Replace phase rows wholesale — Pass 2 supersedes Pass 1, so we
            # delete then re-insert rather than try to diff individual rows.
            phases = envelope.pass2_estimates or envelope.pass1_estimates
            if phases:
                await session.execute(
                    delete(PhaseHistory).where(PhaseHistory.estimate_id == envelope.estimate_id)
                )
                rows = [
                    _phase_row(
                        envelope.estimate_id,
                        p,
                        industry=industry,
                        project_type=project_type,
                        maturity=_codebase_code(stage3),
                    )
                    for p in phases
                ]
                session.add_all([PhaseHistory(**r) for r in rows])
            logger.info(
                "postgres: persisted history for estimate %s (%d phase row(s))",
                envelope.estimate_id,
                len(phases),
            )
        except Exception as exc:  # noqa: BLE001
            # Roll back so session_scope's clean-exit commit is a no-op: otherwise a
            # mid-body failure would leave a partial write to commit, and on asyncpg the
            # poisoned transaction would make that commit re-raise out of this function
            # (breaking the never-raise contract).
            await session.rollback()
            logger.warning("save_estimate_history failed (%s); skipping", exc)
            return


async def list_estimate_history(limit: int = 50) -> list[dict[str, Any]]:
    """Recent persisted estimates (newest first) as summary dicts for the history
    list. Returns [] when Postgres is disabled or unreadable."""
    async with session_scope() as session:
        if session is None:
            return []
        try:
            result = await session.execute(
                select(EstimateHistory)
                .order_by(EstimateHistory.updated_at.desc())
                .limit(limit)
            )
            rows = list(result.scalars().all())
        except Exception as exc:  # noqa: BLE001
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


async def get_estimate_envelope(estimate_id: str) -> dict[str, Any] | None:
    """The full persisted EstimateEnvelope JSON for redisplay, or None when Postgres
    is disabled / the estimate isn't in history / no snapshot was stored."""
    async with session_scope() as session:
        if session is None:
            return None
        try:
            row = await session.get(EstimateHistory, estimate_id)
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.warning("get_estimate_envelope failed (%s)", exc)
            return None
        return row.envelope_json if row else None


# ---------- calibration ----------


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
        except Exception as exc:  # noqa: BLE001
            # _refresh_calibration deletes then re-inserts aggregates; roll back so a
            # failed insert can't leave the delete committed (wiping a phase's
            # aggregates with no replacement) and so the trailing commit can't re-raise.
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
        except Exception as exc:  # noqa: BLE001
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
    out: dict[str, list[dict[str, Any]]] = {}
    for ph in phases:
        out[ph] = await get_calibration(
            ph,
            industry=industry,
            project_type=project_type,
            maturity=_codebase_code(stage3) if stage3 else None,
            limit=per_phase_limit,
        )
    return out


# ---------- reduction bands ----------


async def get_reduction_bands() -> dict[str, dict[str, list[float]]]:
    """Return DB-stored AI-reduction guardrail bands as nested
    ``{phase: {tooling_level: [min, max]}}``.

    Returns an empty dict when Postgres is disabled or the table is empty/unreadable
    — callers (the twins, via parse_input → state) then fall back to the in-code
    ``DEFAULT_BANDS`` in orchestrator/ai_acceleration.py.
    """
    rows = []
    async with session_scope() as session:
        if session is None:
            logger.debug("postgres disabled; no reduction bands (using code defaults)")
            return {}
        try:
            result = await session.execute(select(AiReductionBand))
            rows = list(result.scalars().all())
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.warning("get_reduction_bands failed (%s); using code defaults", exc)
            return {}
    out: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        out.setdefault(r.phase, {})[r.tooling_level] = [r.min_reduction, r.max_reduction]
    return out


async def upsert_reduction_bands(
    items: list[tuple[str, str, float, float]],
) -> bool:
    """Upsert per-(phase, tooling_level) AI-reduction bands (fractions 0..1).

    `items` are ``(phase, tooling_level, min_reduction, max_reduction)``. Each row is
    updated in place or inserted, keyed on (phase, tooling_level). Returns True when
    persisted, False when Postgres is disabled or the write fails — the admin endpoint
    surfaces that so the UI can warn the change wasn't saved.
    """
    try:
        async with session_scope() as session:
            if session is None:
                return False
            existing = {
                (r.phase, r.tooling_level): r
                for r in (await session.execute(select(AiReductionBand))).scalars().all()
            }
            for phase, tooling, lo, hi in items:
                row = existing.get((phase, tooling))
                if row is not None:
                    row.min_reduction = lo
                    row.max_reduction = hi
                else:
                    session.add(
                        AiReductionBand(
                            phase=phase,
                            tooling_level=tooling,
                            min_reduction=lo,
                            max_reduction=hi,
                        )
                    )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("upsert_reduction_bands failed (%s)", exc)
        return False


# ---------- private helpers ----------


def _avg(column):
    from sqlalchemy import func

    return func.avg(column)


def _count(column):
    from sqlalchemy import func

    return func.count(column)


def _reduction_pct(manual_mid: float | None, ai_mid: float | None) -> float:
    if not manual_mid or manual_mid <= 0:
        return 0.0
    ai_mid = ai_mid or 0.0
    if ai_mid >= manual_mid:
        return 0.0
    return float((manual_mid - ai_mid) / manual_mid) * 100.0

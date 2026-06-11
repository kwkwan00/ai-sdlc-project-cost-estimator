"""Repository functions for the Postgres persistence layer.

Three concerns, three small surfaces:

1. `save_estimate_history(envelope, ...)` — denormalize a completed (or in-progress)
   `EstimateEnvelope` into the history tables. Idempotent on `estimate_id` — calling
   it repeatedly during the run upserts the same row. Mirrors Neo4j's
   `save_estimate_envelope` semantics: silently no-ops when Postgres is disabled.

2. `refresh_calibration_for_phase(phase)` — recompute the rolling per-(phase,
   industry, project_type, maturity) aggregates from `phase_history`. Called after
   estimates complete so subsequent runs see the new data.

3. `get_calibration(phase, ...)` — read aggregates for a given phase, optionally
   filtered by industry / project_type / maturity. Returned as plain dicts so callers
   (twins / parse_input) can hand them straight to the LLM prompt without ORM leakage.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select

from db.orm_models import CalibrationAggregate, EstimateHistory, PhaseHistory
from db.postgres_adapter import session_scope
from models.project_schema import EstimateEnvelope, Stage2Context, Stage3Maturity
from models.twin_outputs import DualScenarioEstimate, PhaseEstimate

logger = logging.getLogger(__name__)


# ---------- helpers ----------


def _maturity_for_phase(phase_value: str, stage3: Stage3Maturity | None) -> int | None:
    if stage3 is None:
        return None
    mapping = {
        "discovery": stage3.discovery_maturity,
        "ux_design": stage3.ux_design_maturity,
        "development": stage3.development_maturity,
        "code_review": stage3.code_review_maturity,
        "deployment": stage3.deployment_maturity,
        "qa_testing": stage3.qa_testing_maturity,
    }
    return mapping.get(phase_value)


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
    stage3: Stage3Maturity | None,
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
                        maturity=_maturity_for_phase(p.phase.value, stage3),
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
            logger.warning("save_estimate_history failed (%s); rolling back", exc)
            raise


# ---------- calibration ----------


_ANY = ""  # sentinel for "any value" in CalibrationAggregate dimensions


async def refresh_calibration_for_phase(phase_value: str) -> int:
    """Recompute calibration aggregates for one phase from phase_history.

    Returns the number of aggregate rows written. Skips silently when Postgres is
    disabled. Aggregates are computed for **every** (industry, project_type,
    maturity) triple present in phase_history, plus a single "any" rollup with
    industry="" / project_type="" / maturity_level=0 that twins can use as a fallback.
    """
    async with session_scope() as session:
        if session is None:
            logger.debug(
                "postgres disabled; skipping calibration refresh for phase %s", phase_value
            )
            return 0

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
                    maturity_level=0,
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
                    maturity_level=(r.maturity_level or 0),
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
    """
    async with session_scope() as session:
        if session is None:
            logger.debug(
                "postgres disabled; returning no calibration for phase %s", phase_value
            )
            return []
        result = await session.execute(
            select(CalibrationAggregate).where(CalibrationAggregate.phase == phase_value)
        )
        rows = result.scalars().all()
        if not rows:
            logger.debug("no calibration aggregates stored for phase %s", phase_value)
            return []

        ind = (industry or "").strip()
        ptype = (project_type or "").strip()
        mat = maturity or 0

        def score(r: CalibrationAggregate) -> tuple[int, int]:
            specificity = (
                int(r.industry == ind and ind != "")
                + int(r.project_type == ptype and ptype != "")
                + int(r.maturity_level == mat and mat != 0)
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
                "maturity_level": r.maturity_level or None,
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
    stage3: Stage3Maturity | None = None,
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
            maturity=_maturity_for_phase(ph, stage3) if stage3 else None,
            limit=per_phase_limit,
        )
    return out


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

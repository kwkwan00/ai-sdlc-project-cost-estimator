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
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from db.orm_models import EstimateHistory, LlmCall, PhaseHistory
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
            existing.method = envelope.method
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
            # The estimation flow that produced this row, read from the authoritative
            # estimate_history.method column (migration 0017) — same source as Observability's
            # _by_estimate, so the dashboard badge + WBS-only "Duplicate" action stay consistent
            # with it (a trimmed/older envelope_json blob could lack the key).
            "method": r.method or "twins",
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
    Phase rows AND llm_call rows are deleted explicitly (mirroring `save_estimate_history` /
    `save_llm_calls`) so it works even where the FK cascade isn't enforced (e.g. SQLite in tests).
    Skipping the llm_call delete would orphan those rows: they'd keep summing into the Observability
    grand total (SUM over all rows) while their per-estimate inner-join to a now-deleted history row
    fails — the exact total-vs-per-estimate inconsistency the design avoids."""
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
                delete(LlmCall).where(LlmCall.estimate_id == estimate_id)
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


def _empty_usage() -> dict[str, Any]:
    return {
        "call_count": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cost_usd": 0.0, "by_model": [], "by_agent": [],
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _parse_called_at(at: Any) -> datetime | None:
    if isinstance(at, datetime):
        return at
    if isinstance(at, str):
        try:
            return datetime.fromisoformat(at)
        except ValueError:
            return None
    return None


def _llm_call_row(estimate_id: str | None, c: dict[str, Any], session_id: str | None = None) -> LlmCall:
    return LlmCall(
        estimate_id=estimate_id,
        session_id=session_id,
        agent=str(c.get("agent") or "unknown")[:64],
        model=str(c.get("model") or "")[:64],
        input_tokens=int(c.get("input_tokens") or 0),
        output_tokens=int(c.get("output_tokens") or 0),
        cache_read_tokens=int(c.get("cache_read_tokens") or 0),
        cost_usd=float(c.get("cost_usd") or 0.0),
        called_at=_parse_called_at(c.get("called_at")),
    )


async def save_llm_calls(estimate_id: str, calls: list[dict[str, Any]]) -> None:
    """Replace the per-call `llm_call` rows for an estimate (delete + insert) — one row per LLM call
    (agent, model, tokens, computed cost, timestamp). Best-effort / never-raise. The estimate's
    history row must already be persisted (FK), which the persist seam guarantees.

    The delete is scoped to rows this function owns — those with `session_id IS NULL` (the twin /
    WBS calls it inserts). It must NOT touch the pre-submission rows (`session_id` non-NULL) that
    `associate_llm_calls` reparented onto this estimate: a two-pass twin estimate re-persists here on
    Pass 2 *after* Pass 1 already associated them, and a blanket delete-by-estimate_id would wipe them
    (they'd be gone, not reset to NULL, so association can't recover them) — permanently losing the
    prefill/roster/tooling cost from Observability. The try wraps the `async with` so a commit-time
    error (re-raised by `session_scope`) is caught too, honoring the never-raise contract."""
    try:
        async with session_scope() as session:
            if session is None:
                return
            await session.execute(
                delete(LlmCall).where(
                    LlmCall.estimate_id == estimate_id,
                    LlmCall.session_id.is_(None),
                )
            )
            for c in calls or []:
                session.add(_llm_call_row(estimate_id, c))
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("save_llm_calls failed for %s (%s)", estimate_id, exc)


async def insert_llm_calls(
    calls: list[dict[str, Any]], *, estimate_id: str | None = None, session_id: str | None = None
) -> None:
    """Insert per-call `llm_call` rows (no delete) — used by the pre-submission agents (prefill /
    roster / tooling), which run before an estimate id exists, so `estimate_id` is None. They carry
    the wizard-run `session_id` so they can later be associated with the estimate the wizard produces
    (`associate_llm_calls`). Best-effort / never-raise — the try wraps the `async with` so a
    commit-time error (re-raised by `session_scope`) is caught and doesn't escape the caller's
    `capture_usage_to_db` block (which would turn a transient DB hiccup into an HTTP 500 on the
    pre-submission draft endpoints)."""
    if not calls:
        return
    try:
        async with session_scope() as session:
            if session is None:
                return
            for c in calls:
                session.add(_llm_call_row(estimate_id, c, session_id))
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("insert_llm_calls failed (%s)", exc)


async def associate_llm_calls(session_id: str | None, estimate_id: str) -> None:
    """Link a wizard run's pre-submission calls to the estimate it produced —
    ``UPDATE llm_call SET estimate_id WHERE session_id = … AND estimate_id IS NULL``. Idempotent
    (already-linked rows no longer match), no-op when `session_id` is falsy. Best-effort / never-raise.
    Must run AFTER the estimate's history row exists (the FK target)."""
    if not session_id:
        return
    async with session_scope() as session:
        if session is None:
            return
        try:
            await session.execute(
                update(LlmCall)
                .where(LlmCall.session_id == session_id, LlmCall.estimate_id.is_(None))
                .values(estimate_id=estimate_id)
            )
        except (SQLAlchemyError, OSError) as exc:
            await session.rollback()
            logger.warning("associate_llm_calls failed for session %s (%s)", session_id, exc)


def calls_from_summary(llm_usage: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Derive per-agent `llm_call` rows from an estimate's `llm_usage` *summary* (its `by_agent`
    breakdown), for when the raw per-call records aren't available — the WBS commit (only the rollup
    is carried through) and backfilling envelopes persisted before the table existed. Each agent
    collapses to one row (for the WBS planner that's its single call); falls back to `by_model` for
    pre-per-agent envelopes."""
    if not llm_usage:
        return []
    by_agent = llm_usage.get("by_agent") or []
    if by_agent:
        return [
            {
                "agent": a.get("agent") or "unknown", "model": a.get("model") or "",
                "input_tokens": a.get("input_tokens") or 0, "output_tokens": a.get("output_tokens") or 0,
                "cache_read_tokens": a.get("cache_read_tokens") or 0, "cost_usd": a.get("cost_usd") or 0.0,
                "called_at": a.get("started_at"),
            }
            for a in by_agent
        ]
    return [
        {
            "agent": "unknown", "model": m.get("model") or "",
            "input_tokens": m.get("input_tokens") or 0, "output_tokens": m.get("output_tokens") or 0,
            "cache_read_tokens": m.get("cache_read_tokens") or 0, "cost_usd": m.get("cost_usd") or 0.0,
            "called_at": None,
        }
        for m in llm_usage.get("by_model") or []
    ]


def _sum_cols() -> list:
    """Fresh SUM/COUNT aggregate expressions over the llm_call numeric columns (one set per query)."""
    return [
        func.count(LlmCall.id),
        func.coalesce(func.sum(LlmCall.input_tokens), 0),
        func.coalesce(func.sum(LlmCall.output_tokens), 0),
        func.coalesce(func.sum(LlmCall.cache_read_tokens), 0),
        func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
    ]


async def _grouped_breakdown(session: Any, col: Any, key: str) -> list[dict[str, Any]]:
    """A DB-side `GROUP BY <col>` SUM breakdown (by_model / by_agent), cost-descending."""
    rows = (
        await session.execute(
            select(col, *_sum_cols())
            .group_by(col)
            .order_by(func.coalesce(func.sum(LlmCall.cost_usd), 0.0).desc())
        )
    ).all()
    return [
        {
            key: r[0] or "?", "calls": int(r[1] or 0), "input_tokens": int(r[2] or 0),
            "output_tokens": int(r[3] or 0), "cache_read_tokens": int(r[4] or 0),
            "cost_usd": round(float(r[5] or 0.0), 4),
        }
        for r in rows
    ]


def _llm_usage_from_agent_rows(agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble one estimate's LlmUsage dict from its per-(agent, model) rows (by_agent folded to one
    row per agent + by_model regrouped + totals)."""
    by_model: dict[str, dict[str, Any]] = {}
    by_agent: dict[str, dict[str, Any]] = {}
    cc = it = ot = crt = 0
    cost = 0.0
    scalars = ("calls", "input_tokens", "output_tokens", "cache_read_tokens")
    for a in agents:
        cc += a["calls"]
        it += a["input_tokens"]
        ot += a["output_tokens"]
        crt += a["cache_read_tokens"]
        cost += a["cost_usd"]
        m = by_model.setdefault(
            a["model"],
            {"model": a["model"], "calls": 0, "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cost_usd": 0.0},
        )
        for k in scalars:
            m[k] += a[k]
        m["cost_usd"] = round(m["cost_usd"] + a["cost_usd"], 4)
        # Fold by agent NAME — `_by_estimate` groups by (estimate_id, agent, model), so one agent that
        # ran under two models would otherwise yield two rows sharing the same `agent` key (a React
        # duplicate-key + split row on the Observability page). Keep the first model seen (matching
        # `usage.summarize_usage`) and widen the call span.
        ag = by_agent.get(a["agent"])
        if ag is None:
            by_agent[a["agent"]] = {**a}
        else:
            for k in scalars:
                ag[k] += a[k]
            ag["cost_usd"] = round(ag["cost_usd"] + a["cost_usd"], 4)
            spans = [v for v in (ag["started_at"], a["started_at"]) if v]
            ends = [v for v in (ag["finished_at"], a["finished_at"]) if v]
            ag["started_at"] = min(spans) if spans else None
            ag["finished_at"] = max(ends) if ends else None
    return {
        "call_count": cc, "input_tokens": it, "output_tokens": ot, "cache_read_tokens": crt,
        "cost_usd": round(cost, 4),
        "by_model": sorted(by_model.values(), key=lambda m: m["cost_usd"], reverse=True),
        "by_agent": sorted(by_agent.values(), key=lambda a: a["cost_usd"], reverse=True),
    }


async def _by_estimate(session: Any, limit: int) -> list[dict[str, Any]]:
    """Per-estimate breakdown: a DB-side `GROUP BY (estimate_id, agent, model)` joined to the estimate
    (newest first), assembled into one LlmUsage per estimate, capped at `limit` estimates."""
    # Restrict to the newest `limit` estimates (that have calls) in SQL, so the per-(estimate, agent,
    # model) GROUP BY never materializes the whole llm_call table just to discard the tail in Python.
    newest_ids = (
        select(LlmCall.estimate_id)
        .join(EstimateHistory, EstimateHistory.id == LlmCall.estimate_id)
        .group_by(LlmCall.estimate_id, EstimateHistory.updated_at)
        .order_by(EstimateHistory.updated_at.desc())
        .limit(limit)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                LlmCall.estimate_id, LlmCall.agent, LlmCall.model, *_sum_cols(),
                func.min(LlmCall.called_at), func.max(LlmCall.called_at),
                EstimateHistory.project_name, EstimateHistory.created_at, EstimateHistory.method,
            )
            .join(EstimateHistory, EstimateHistory.id == LlmCall.estimate_id)
            .where(LlmCall.estimate_id.in_(select(newest_ids.c.estimate_id)))
            .group_by(
                LlmCall.estimate_id, LlmCall.agent, LlmCall.model,
                EstimateHistory.project_name, EstimateHistory.created_at,
                EstimateHistory.updated_at, EstimateHistory.method,
            )
            .order_by(EstimateHistory.updated_at.desc())
        )
    ).all()

    order: list[str] = []
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        est_id = r[0]
        if est_id not in seen:
            if len(seen) >= limit:
                continue
            order.append(est_id)
            seen[est_id] = {
                "project_name": r[10] or "", "created_at": _iso(r[11]),
                "method": r[12] or "twins", "agents": [],
            }
        seen[est_id]["agents"].append(
            {
                "agent": r[1], "model": r[2], "calls": int(r[3] or 0), "input_tokens": int(r[4] or 0),
                "output_tokens": int(r[5] or 0), "cache_read_tokens": int(r[6] or 0),
                "cost_usd": round(float(r[7] or 0.0), 4), "started_at": _iso(r[8]), "finished_at": _iso(r[9]),
            }
        )

    result: list[dict[str, Any]] = []
    for est_id in order:
        est = seen[est_id]
        # Flow comes straight from the estimate_history.method column (authoritative) — no longer
        # inferred from whether a `propose_wbs` agent row happened to be captured, which mislabeled a
        # WBS estimate as "twins" whenever the planner usage wasn't persisted.
        result.append(
            {
                "estimate_id": est_id,
                "project_name": est["project_name"],
                "created_at": est["created_at"],
                "method": est["method"],
                "llm_usage": _llm_usage_from_agent_rows(est["agents"]),
            }
        )
    return result


async def aggregate_llm_usage(limit: int = 500) -> dict[str, Any]:
    """Aggregate per-call LLM usage from the `llm_call` table for the Observability page.

    The grand total + per-model + per-agent breakdowns are computed **DB-side** (SUM / GROUP BY); the
    newest-first per-estimate list is assembled from a per-(estimate, agent) GROUP BY joined to the
    estimate row. Returns the empty case when Postgres is disabled / unreadable; never raises."""
    empty: dict[str, Any] = {"total": _empty_usage(), "by_estimate": []}
    async with session_scope() as session:
        if session is None:
            return empty
        try:
            total = _empty_usage()
            total["by_model"] = await _grouped_breakdown(session, LlmCall.model, "model")
            total["by_agent"] = await _grouped_breakdown(session, LlmCall.agent, "agent")
            # Grand total folds the by_model breakdown — no separate full-table SUM scan needed
            # (the two are identical aggregations; one pass instead of N+1).
            total.update(
                call_count=sum(m["calls"] for m in total["by_model"]),
                input_tokens=sum(m["input_tokens"] for m in total["by_model"]),
                output_tokens=sum(m["output_tokens"] for m in total["by_model"]),
                cache_read_tokens=sum(m["cache_read_tokens"] for m in total["by_model"]),
                cost_usd=round(sum(m["cost_usd"] for m in total["by_model"]), 4),
            )
            by_estimate = await _by_estimate(session, limit)
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError) as exc:
            await session.rollback()
            logger.warning("aggregate_llm_usage failed (%s)", exc)
            return empty
    return {"total": total, "by_estimate": by_estimate}

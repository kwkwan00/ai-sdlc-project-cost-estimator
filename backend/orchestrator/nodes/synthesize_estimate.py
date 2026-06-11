"""synthesize_estimate — aggregate per-phase outputs into the final DualScenarioEstimate."""

from __future__ import annotations

import logging
from collections import defaultdict

from models.estimation_state import EstimationState
from models.project_schema import RoleRoster
from models.twin_outputs import (
    DualScenarioEstimate,
    HourRange,
    PhaseEstimate,
    RoleHeadcount,
)
from observability.langfuse_wrapper import traced

logger = logging.getLogger(__name__)

# Effective work hours per person per week (40 - meetings / context switching).
WORK_HOURS_PER_WEEK = 32


def _sum_range(phases: list[PhaseEstimate], ai: bool) -> HourRange:
    if not phases:
        return HourRange(optimistic=0, most_likely=0, pessimistic=0)
    o = sum((p.ai_assisted_hours.optimistic if ai else p.manual_only_hours.optimistic) for p in phases)
    m = sum((p.ai_assisted_hours.most_likely if ai else p.manual_only_hours.most_likely) for p in phases)
    pess = sum((p.ai_assisted_hours.pessimistic if ai else p.manual_only_hours.pessimistic) for p in phases)
    return HourRange(optimistic=o, most_likely=m, pessimistic=pess)


def _sum_hours_by_role(phases: list[PhaseEstimate], ai: bool) -> dict[str, float]:
    """Aggregate per-role hours across all phases. Keyed on role_id."""
    totals: dict[str, float] = defaultdict(float)
    for p in phases:
        rows = p.ai_assisted_role_hours if ai else p.manual_only_role_hours
        for rh in rows:
            totals[rh.role_id] += rh.hours
    return dict(totals)


@traced(name="synthesize_estimate")
async def synthesize_estimate(state: EstimationState) -> dict:
    pass2: list[PhaseEstimate] = state.get("pass2_estimates", [])
    parsed = state.get("parsed_context", {})
    stage2 = state.get("stage2")
    roster = stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()

    target_weeks = (stage2.target_timeline_weeks if stage2 and stage2.target_timeline_weeks else 0)

    ai_range = _sum_range(pass2, ai=True)
    manual_range = _sum_range(pass2, ai=False)

    ai_hours_by_role = _sum_hours_by_role(pass2, ai=True)
    rate_by_role = {r.role_id: r.rate_per_hour for r in roster.roles}

    headcount_rows: list[RoleHeadcount] = []
    weekly_burn = 0.0

    if target_weeks > 0:
        capacity = target_weeks * WORK_HOURS_PER_WEEK
        for r in roster.roles:
            role_hours = ai_hours_by_role.get(r.role_id, 0.0)
            if role_hours <= 0:
                hc = 0
            else:
                # Ceiling so we always have enough capacity to deliver in the
                # target window. Minimum of 1 if the role has any work at all.
                hc = max(1, int((role_hours / capacity) + 0.99))
            headcount_rows.append(
                RoleHeadcount(
                    role_id=r.role_id,
                    role_description=r.description,
                    category=r.category,
                    seniority=r.seniority,
                    headcount=hc,
                )
            )
            weekly_burn += hc * WORK_HOURS_PER_WEEK * rate_by_role.get(r.role_id, 0.0)

        duration_low = max(1.0, target_weeks * 0.85)
        duration_high = target_weeks * 1.25
    else:
        # No target: derive duration assuming a default 5-person team.
        default_team_capacity = 5 * WORK_HOURS_PER_WEEK
        duration_low = ai_range.optimistic / default_team_capacity if default_team_capacity else 0
        duration_high = ai_range.pessimistic / default_team_capacity if default_team_capacity else 0
        for r in roster.roles:
            role_hours = ai_hours_by_role.get(r.role_id, 0.0)
            headcount_rows.append(
                RoleHeadcount(
                    role_id=r.role_id,
                    role_description=r.description,
                    category=r.category,
                    seniority=r.seniority,
                    headcount=1 if role_hours > 0 else 0,
                )
            )

    avg_confidence = (
        sum(p.confidence for p in pass2) / len(pass2) if pass2 else 0.0
    )

    final = DualScenarioEstimate(
        total_ai_assisted_hours=ai_range,
        total_manual_only_hours=manual_range,
        ai_hours_saved_pert=manual_range.pert_mean - ai_range.pert_mean,
        ai_cost_saved_usd=(
            parsed.get("total_cost_manual_only_usd", 0.0)
            - parsed.get("total_cost_ai_assisted_usd", 0.0)
        ),
        phases=pass2,
        confidence=avg_confidence,
        duration_weeks_low=duration_low,
        duration_weeks_high=duration_high,
        headcount_by_role=headcount_rows,
        weekly_burn_rate_usd=weekly_burn,
        total_cost_ai_assisted_usd=parsed.get("total_cost_ai_assisted_usd", 0.0),
        total_cost_manual_only_usd=parsed.get("total_cost_manual_only_usd", 0.0),
    )
    logger.info(
        "synthesize_estimate complete: ai_assisted=%.0fh manual_only=%.0fh (PERT) across %d phase(s); %d role(s) in headcount",
        ai_range.pert_mean,
        manual_range.pert_mean,
        len(pass2),
        len(headcount_rows),
    )
    return {"final_estimate": final}

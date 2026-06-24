"""synthesize_estimate — aggregate per-phase outputs into the final DualScenarioEstimate."""

from __future__ import annotations

import logging
import math
import os
from collections import defaultdict
from dataclasses import dataclass

from db.repositories import get_staffing_coefficients
from models.estimation_state import EstimationState
from models.project_schema import CustomRole, RoleRoster, Stage2Context
from models.twin_outputs import (
    DualScenarioEstimate,
    HourRange,
    PhaseEstimate,
    RoleHeadcount,
)
from observability.langfuse_wrapper import traced
from orchestrator.nodes._twin_base import rate_by_role
from orchestrator.staffing import (
    coordination_overhead,
    optimal_team_size,
    staffing_efficiency,
    team_throughput,
)

logger = logging.getLogger(__name__)

# Work hours per person per week. Matches the EFFORT basis: COCOMO emits hours at 152/PM
# (8h/day × 19 days), so headcount capacity must use the same full work-week (8h/day × 5).
# A lower "productive" number here would double-count meeting overhead and over-count headcount.
WORK_HOURS_PER_WEEK = 40

# Default cross-phase correlation: phases partially co-move (shared scope drives
# several phases together). Read at call time via _phase_correlation() so the
# PHASE_CORRELATION env var can override it without re-importing the module.
PHASE_CORRELATION = 0.3


def _phase_correlation() -> float:
    """Cross-phase correlation ρ ∈ [0, 1] used to combine per-phase std's.

    Phases partially co-move (a larger-than-expected scope drives several phases —
    discovery, dev, QA — up together), so the project-total spread sits between two
    extremes: ρ=0 treats phases as INDEPENDENT (variances add in quadrature, today's
    behavior) and ρ=1 treats them as COMONOTONIC (perfectly correlated; std's add
    linearly). Default 0.3 (mild positive co-movement); override with the
    ``PHASE_CORRELATION`` env var. Clamped to [0, 1]."""
    try:
        rho = float(os.getenv("PHASE_CORRELATION", str(PHASE_CORRELATION)))
    except ValueError:
        return PHASE_CORRELATION
    return min(1.0, max(0.0, rho))
# Standard-normal quantiles for the fan-chart percentiles, hard-coded so the
# project-total lognormal fit needs no scipy. Keys mirror HourRange.percentiles
# and the per-phase Monte Carlo output (montecarlo._PCTS).
_Z: dict[str, float] = {
    "p5": -1.6449,
    "p10": -1.2816,
    "p25": -0.6745,
    "p50": 0.0,
    "p75": 0.6745,
    "p90": 1.2816,
    "p95": 1.6449,
}


def _lognormal_band(
    mean: float, std: float, anchor: float
) -> tuple[float, float, dict[str, float] | None]:
    """Fit a lognormal to ``(mean, std)`` and return ``(optimistic, pessimistic,
    percentiles)`` for the project total — the P10/P90 fan-chart band.

    ``anchor`` is the deterministic most-likely total used to keep the band sane
    in the degenerate guards. Pure stdlib (``math``), no scipy. Guards:

    - ``mean <= 1e-9``  → no usable lognormal; return a symmetric ±std band around
      the anchor (clamped at 0), percentiles ``None``.
    - ``cv^2 <= 1e-9``  → effectively zero spread; collapse to the point
      (lo = hi = anchor), percentiles ``None``.
    - otherwise         → method-of-moments lognormal: ``sigma = sqrt(log1p(cv^2))``,
      ``mu = log(mean) - 0.5*sigma^2``, ``band(z) = exp(mu + z*sigma)``; optimistic
      is P10, pessimistic is P90, and the full ``_Z`` vector is materialized.
    """
    if mean <= 1e-9:
        lo = max(0.0, anchor - std)
        hi = anchor + std
        return lo, hi, None

    cv2 = (std / mean) ** 2
    if cv2 <= 1e-9:
        return anchor, anchor, None

    sigma = math.sqrt(math.log1p(cv2))
    mu = math.log(mean) - 0.5 * sigma * sigma

    def band(z: float) -> float:
        return math.exp(mu + z * sigma)

    percentiles = {name: band(z) for name, z in _Z.items()}
    return band(_Z["p10"]), band(_Z["p90"]), percentiles


def _combine_std(stds: list[float], rho: float) -> float:
    """Combine per-phase standard deviations under a uniform pairwise correlation ρ.

    A correlation-ρ portfolio variance is ``Σ std_i² + ρ·Σ_{i≠j} std_i·std_j``, which
    factors into the convex blend

        total_std = sqrt( (1−ρ)·Σ std_i²  +  ρ·(Σ std_i)² ).

    ρ=0 → INDEPENDENCE (variances add in quadrature, ``sqrt(Σ std_i²)``); ρ=1 →
    COMONOTONIC (perfect correlation, std's add linearly, ``Σ std_i``). The blend is
    monotonic in ρ, so any 0<ρ<1 lands strictly between the two extremes."""
    sum_sq = sum(s * s for s in stds)
    sum_lin = sum(stds)
    var = (1.0 - rho) * sum_sq + rho * sum_lin * sum_lin
    return math.sqrt(max(0.0, var))


def _combine_range(
    phases: list[PhaseEstimate], ai: bool, *, rho: float | None = None
) -> HourRange:
    """Combine the per-phase hour ranges into the project total.

    ``most_likely`` is ALWAYS the comonotonic sum of the per-phase deterministic
    mids (Σ most_likely). For the band:

    - **Monte Carlo path** (every phase range carries ``std``): sum the means and
      blend the per-phase std's under a cross-phase correlation ρ (see
      ``_combine_std``) — ``total_std = sqrt((1−ρ)·Σstd_i² + ρ·(Σstd_i)²)`` — then
      derive optimistic(P10)/pessimistic(P90) + the fan-chart ``percentiles`` from a
      guarded lognormal fit of the combined ``(mean, std)``. ρ defaults to
      ``_phase_correlation()`` (``PHASE_CORRELATION``, env-overridable). ρ=0 is the
      pure independence combine (variances add in quadrature → narrower than the
      comonotonic sum); ρ=1 is the comonotonic linear-std sum; 0<ρ<1 sits between.
    - **Stub / legacy path** (any phase range lacks ``std``): fall back to the EXACT
      comonotonic sum of each percentile (Σ optimistic, Σ most_likely, Σ
      pessimistic), with ``std``/``mean``/``percentiles`` left ``None``. Guarantees
      no behavior change on the deterministic stub path.
    """
    if not phases:
        return HourRange(optimistic=0, most_likely=0, pessimistic=0)

    rho = _phase_correlation() if rho is None else min(1.0, max(0.0, rho))
    ranges = [(p.ai_assisted_hours if ai else p.manual_only_hours) for p in phases]
    total_ml = sum(r.most_likely for r in ranges)

    if all(r.std is not None for r in ranges):
        # Variances combine under cross-phase correlation ρ (ρ=0 → independence).
        total_mean = sum((r.mean if r.mean is not None else r.pert_mean) for r in ranges)
        total_std = _combine_std([r.std or 0.0 for r in ranges], rho)
        optimistic, pessimistic, percentiles = _lognormal_band(total_mean, total_std, total_ml)
        # Keep the deterministic mid inside the band (mirrors result_to_hour_range
        # so HourRange's ordering-coercion can't silently demote total_ml).
        most_likely = min(max(total_ml, optimistic), pessimistic)
        return HourRange(
            optimistic=max(0.0, optimistic),
            most_likely=max(0.0, most_likely),
            pessimistic=max(0.0, pessimistic),
            std=max(0.0, total_std),
            mean=max(0.0, total_mean),
            percentiles=(
                {k: max(0.0, v) for k, v in percentiles.items()}
                if percentiles is not None
                else None
            ),
        )

    # Stub / legacy: comonotonic sum (exact prior behavior).
    o = sum(r.optimistic for r in ranges)
    pess = sum(r.pessimistic for r in ranges)
    return HourRange(optimistic=o, most_likely=total_ml, pessimistic=pess)


def _sum_hours_by_role(phases: list[PhaseEstimate], ai: bool) -> dict[str, float]:
    """Aggregate per-role hours across all phases. Keyed on role_id."""
    totals: dict[str, float] = defaultdict(float)
    for p in phases:
        rows = p.ai_assisted_role_hours if ai else p.manual_only_role_hours
        for rh in rows:
            totals[rh.role_id] += rh.hours
    return dict(totals)


def _distribute_team(
    total: int, hours_by_role: dict[str, float], roles: list[CustomRole]
) -> dict[str, int]:
    """Distribute a total team size across the roster by effort share: every role with work gets
    at least one person, and the remainder goes to the highest-effort roles (largest remainder).
    Bumps ``total`` up to the number of active roles if needed, so no working role is left
    unstaffed. Returns ``{role_id: headcount}`` summing to ``max(total, #active roles)``."""
    alloc: dict[str, int] = {r.role_id: 0 for r in roles}
    active = [r for r in roles if hours_by_role.get(r.role_id, 0.0) > 0]
    if not active or total <= 0:
        return alloc
    total = max(total, len(active))
    for r in active:
        alloc[r.role_id] = 1
    remainder = total - len(active)
    if remainder > 0:
        total_hours = sum(hours_by_role.get(r.role_id, 0.0) for r in active) or 1.0
        ideals = [
            (r.role_id, remainder * hours_by_role.get(r.role_id, 0.0) / total_hours)
            for r in active
        ]
        for rid, ideal in ideals:
            alloc[rid] += int(ideal)
        leftover = remainder - sum(int(ideal) for _, ideal in ideals)
        for rid, _ in sorted(ideals, key=lambda x: x[1] - int(x[1]), reverse=True)[:leftover]:
            alloc[rid] += 1
    return alloc


def _headcounts_for_target(
    roster: RoleRoster, ai_hours_by_role: dict[str, float], target_weeks: float
) -> dict[str, int]:
    """Deadline-derived headcount per role: ``ceil(role_hours / (target_weeks · WORK_HOURS))``,
    ≥ 1 for any role with work, 0 otherwise — enough capacity to deliver in the target window.
    Only called when ``target_weeks > 0`` (so capacity is positive)."""
    capacity = target_weeks * WORK_HOURS_PER_WEEK
    out: dict[str, int] = {}
    for r in roster.roles:
        role_hours = ai_hours_by_role.get(r.role_id, 0.0)
        out[r.role_id] = 0 if role_hours <= 0 else max(1, math.ceil(role_hours / capacity))
    return out


def _headcount_row(
    r: CustomRole,
    headcount: int,
    *,
    rates: dict[str, float],
    ai_hours_by_role: dict[str, float],
    manual_hours_by_role: dict[str, float],
) -> RoleHeadcount:
    """One staffing/cost table row for a roster role (hours × rate, both scenarios)."""
    rate = rates.get(r.role_id, 0.0)
    ai_h = ai_hours_by_role.get(r.role_id, 0.0)
    manual_h = manual_hours_by_role.get(r.role_id, 0.0)
    return RoleHeadcount(
        role_id=r.role_id,
        role_description=r.description,
        category=r.category,
        seniority=r.seniority,
        headcount=headcount,
        rate_per_hour=rate,
        ai_assisted_hours=ai_h,
        manual_only_hours=manual_h,
        ai_assisted_cost_usd=ai_h * rate,
        manual_only_cost_usd=manual_h * rate,
    )


@dataclass
class _StaffingPlan:
    """One coherent team: the headcount table, its size + overhead, weekly burn, and the schedule
    (which already embeds the Brooks overhead — explicitly in the target regime, via
    ``team_throughput``'s ``(1−o(n))`` otherwise)."""

    headcount_rows: list[RoleHeadcount]
    weekly_burn: float
    team_size: int
    optimal_team_size: int
    overhead: float
    duration_low: float
    duration_high: float


def _resolve_staffing(
    roster: RoleRoster,
    *,
    ai_hours_by_role: dict[str, float],
    manual_hours_by_role: dict[str, float],
    rates: dict[str, float],
    ai_range: HourRange,
    target_weeks: int,
    coeffs: dict[str, float],
) -> _StaffingPlan:
    """Team-scaling (Brooks's Law + diminishing returns) at the project level (so the six twins stay
    independent): the headcount table, team size, coordination overhead, weekly burn, and schedule —
    all describing ONE coherent team. With a target timeline, headcount = deadline-derived ceilings
    and the overhead stretches that window; otherwise the recommended team ``opt`` distributed by
    effort share, with the duration derived from that team's throughput."""
    opt = optimal_team_size(ai_range.most_likely, WORK_HOURS_PER_WEEK, coeffs)

    # Per-regime headcount: deadline-derived ceilings with a target, else `opt` distributed across
    # the roster by effort share (≥ 1 per active role). Both yield a {role_id: headcount} map.
    if target_weeks > 0:
        headcounts = _headcounts_for_target(roster, ai_hours_by_role, target_weeks)
    else:
        headcounts = _distribute_team(opt, ai_hours_by_role, roster.roles)

    headcount_rows = [
        _headcount_row(
            r,
            headcounts.get(r.role_id, 0),
            rates=rates,
            ai_hours_by_role=ai_hours_by_role,
            manual_hours_by_role=manual_hours_by_role,
        )
        for r in roster.roles
    ]
    weekly_burn = sum(
        headcounts.get(r.role_id, 0) * WORK_HOURS_PER_WEEK * rates.get(r.role_id, 0.0)
        for r in roster.roles
    )
    team_size = sum(r.headcount for r in headcount_rows)
    overhead = coordination_overhead(team_size, coeffs)

    if target_weeks > 0:
        # Brooks: coordination overhead stretches the target window.
        duration_low = max(1.0, target_weeks * 0.85) * (1.0 + overhead)
        duration_high = target_weeks * 1.25 * (1.0 + overhead)
    else:
        # No target: derive the duration from the staffed team's throughput (both laws), so the
        # table / overhead / burn / duration all describe ONE team (no phantom decoupled size).
        # A degenerate team (team_size 0 → throughput 0) would otherwise report a 0-week schedule;
        # floor the divisor at a minimal 1-person throughput so the duration stays a real estimate.
        weekly_throughput = team_throughput(team_size, coeffs) * WORK_HOURS_PER_WEEK
        if weekly_throughput <= 0:
            weekly_throughput = max(
                team_throughput(1, coeffs) * WORK_HOURS_PER_WEEK, WORK_HOURS_PER_WEEK
            )
        duration_low = ai_range.optimistic / weekly_throughput
        duration_high = ai_range.pessimistic / weekly_throughput

    return _StaffingPlan(
        headcount_rows=headcount_rows,
        weekly_burn=weekly_burn,
        team_size=team_size,
        optimal_team_size=opt,
        overhead=overhead,
        duration_low=duration_low,
        duration_high=duration_high,
    )


def _apply_cost_and_duration_uplifts(
    *,
    cost_ai: float,
    cost_manual: float,
    duration_low: float,
    duration_high: float,
    overhead: float,
    contingency_pct: float,
) -> tuple[float, float, float, float]:
    """Apply the two project-level uplifts and return ``(cost_ai, cost_manual, dur_low, dur_high)``:

    1. **Brooks coordination tax** on cost (both scenarios; ``commercial_processing`` kept the base
       labor cost). NOT on duration — the schedule already embeds the overhead. Diminishing returns
       does NOT inflate cost (the algorithm estimates already embed a normal team's productivity —
       see orchestrator/staffing.py).
    2. **Contingency reserve** — a deliberate admin-set management buffer (distinct from the Monte
       Carlo band) on BOTH cost scenarios AND the timeline. Hours / role-hours / headcount are
       intentionally untouched (so the twin eval rubrics are unaffected). 0% → no-op.

    ``contingency_pct`` is assumed already clamped ``≥ 0`` by the caller. The operation order is
    preserved exactly so results are bit-identical to the prior inline form."""
    cost_ai *= 1.0 + overhead
    cost_manual *= 1.0 + overhead
    contingency = contingency_pct / 100.0
    cost_ai *= 1.0 + contingency
    cost_manual *= 1.0 + contingency
    duration_low *= 1.0 + contingency
    duration_high *= 1.0 + contingency
    return cost_ai, cost_manual, duration_low, duration_high


async def synthesize_from_phase_estimates(
    phases: list[PhaseEstimate],
    *,
    stage2: Stage2Context | None,
    total_cost_ai_assisted_usd: float,
    total_cost_manual_only_usd: float,
    contingency_pct: float = 0.0,
    consistency_warnings: list[str] | None = None,
) -> DualScenarioEstimate:
    """Typed core of the synthesize tail: combine per-phase ``PhaseEstimate``s + their base labor
    costs into the final ``DualScenarioEstimate``. A thin coordinator over ``_combine_range``
    (variance-combine), ``_resolve_staffing`` (Brooks + diminishing-returns team/headcount/schedule),
    and ``_apply_cost_and_duration_uplifts`` (overhead + contingency). Shared by the graph node
    ``synthesize_estimate`` (which adapts the untyped ``EstimationState``) and the WBS rollup — so
    neither hand-builds a state dict or needs a ``# type: ignore``."""
    pass2 = phases
    consistency_warnings = consistency_warnings or []
    contingency_pct = max(0.0, contingency_pct)
    roster = stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()
    target_weeks = stage2.target_timeline_weeks if stage2 and stage2.target_timeline_weeks else 0

    ai_range = _combine_range(pass2, ai=True)
    manual_range = _combine_range(pass2, ai=False)
    ai_hours_by_role = _sum_hours_by_role(pass2, ai=True)
    manual_hours_by_role = _sum_hours_by_role(pass2, ai=False)
    rates = rate_by_role(roster)
    coeffs = await get_staffing_coefficients()

    plan = _resolve_staffing(
        roster,
        ai_hours_by_role=ai_hours_by_role,
        manual_hours_by_role=manual_hours_by_role,
        rates=rates,
        ai_range=ai_range,
        target_weeks=target_weeks,
        coeffs=coeffs,
    )
    cost_ai, cost_manual, duration_low, duration_high = _apply_cost_and_duration_uplifts(
        cost_ai=total_cost_ai_assisted_usd,
        cost_manual=total_cost_manual_only_usd,
        duration_low=plan.duration_low,
        duration_high=plan.duration_high,
        overhead=plan.overhead,
        contingency_pct=contingency_pct,
    )

    avg_confidence = sum(p.confidence for p in pass2) / len(pass2) if pass2 else 0.0

    final = DualScenarioEstimate(
        total_ai_assisted_hours=ai_range,
        total_manual_only_hours=manual_range,
        ai_hours_saved_pert=manual_range.pert_mean - ai_range.pert_mean,
        ai_cost_saved_usd=cost_manual - cost_ai,
        phases=pass2,
        confidence=avg_confidence,
        duration_weeks_low=duration_low,
        duration_weeks_high=duration_high,
        headcount_by_role=plan.headcount_rows,
        weekly_burn_rate_usd=plan.weekly_burn,
        brooks_overhead_pct=round(plan.overhead * 100.0, 1),
        contingency_pct=round(contingency_pct, 1),
        staffing_efficiency_pct=round(staffing_efficiency(plan.team_size, coeffs) * 100.0, 1),
        team_size=plan.team_size,
        optimal_team_size=plan.optimal_team_size,
        total_cost_ai_assisted_usd=cost_ai,
        total_cost_manual_only_usd=cost_manual,
        consistency_warnings=consistency_warnings,
    )
    logger.info(
        "synthesize determine: combined %d phase(s) [%s] → ai=%.0fh manual=%.0fh (most likely); "
        "Brooks overhead=%.1f%% contingency=%.1f%% team_size=%d (optimal=%d) → "
        "cost ai=$%.0f manual=$%.0f duration=%.0f–%.0fwk; %d role(s) in headcount",
        len(pass2),
        "MC variance-combine" if ai_range.std is not None else "comonotonic",
        ai_range.most_likely,
        manual_range.most_likely,
        plan.overhead * 100.0,
        contingency_pct,
        plan.team_size,
        plan.optimal_team_size,
        cost_ai,
        cost_manual,
        duration_low,
        duration_high,
        len(plan.headcount_rows),
    )
    return final


@traced(name="synthesize_estimate")
async def synthesize_estimate(state: EstimationState) -> dict:
    """Graph node: adapt the untyped ``EstimationState`` onto the typed synthesize core."""
    final = await synthesize_from_phase_estimates(
        state.get("pass2_estimates", []),
        stage2=state.get("stage2"),
        total_cost_ai_assisted_usd=state.get("total_cost_ai_assisted_usd", 0.0),
        total_cost_manual_only_usd=state.get("total_cost_manual_only_usd", 0.0),
        contingency_pct=state.get("contingency_pct", 0.0),
        consistency_warnings=state.get("consistency_warnings", []),
    )
    return {"final_estimate": final}

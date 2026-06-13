"""commercial_processing — applies the rate table to twin outputs to produce costs.

MVP: simple labor-cost calculation. No PM overhead / contingency yet (those are
post-MVP knobs per planning outline §7).

Rates are pulled from the user's roster (Stage 2's `RoleRoster`). Each phase's
`*_role_hours` list carries `role_id`; we look the role up in the roster to find
its rate. Roles missing from the roster (shouldn't happen — `attribute_roles`
emits one entry per roster role) fall through at $0/h.
"""

from __future__ import annotations

import logging

from models.estimation_state import EstimationState
from models.project_schema import RoleRoster
from models.twin_outputs import PhaseEstimate, RoleHours
from observability.langfuse_wrapper import traced

logger = logging.getLogger(__name__)


def _phase_cost(phase: PhaseEstimate, rate_by_role: dict[str, float], ai: bool) -> float:
    rows: list[RoleHours] = (
        phase.ai_assisted_role_hours if ai else phase.manual_only_role_hours
    )
    return sum(rh.hours * rate_by_role.get(rh.role_id, 0.0) for rh in rows)


@traced(name="commercial_processing")
async def commercial_processing(state: EstimationState) -> dict:
    pass2 = state.get("pass2_estimates", [])
    stage2 = state.get("stage2")
    roster = stage2.roster if stage2 and stage2.roster.roles else RoleRoster.default()

    rate_by_role = {r.role_id: r.rate_per_hour for r in roster.roles}

    total_ai_cost = sum(_phase_cost(p, rate_by_role, ai=True) for p in pass2)
    total_manual_cost = sum(_phase_cost(p, rate_by_role, ai=False) for p in pass2)

    logger.info(
        "commercial_processing complete: %d role(s) priced; cost ai_assisted=$%.0f manual_only=$%.0f",
        len(rate_by_role),
        total_ai_cost,
        total_manual_cost,
    )
    return {
        "total_cost_ai_assisted_usd": total_ai_cost,
        "total_cost_manual_only_usd": total_manual_cost,
    }

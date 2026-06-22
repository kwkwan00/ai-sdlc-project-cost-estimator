"""Deterministic bottom-up rollup of a WBS tree into a DualScenarioEstimate.

The WBS flow's compute core. It reuses the existing uncertainty + synthesis stack rather than
reinventing any math:

1. Group the tree's leaves by `Phase`.
2. Per phase, build ONE `PhaseEstimate`: combine the leaf 3-point bands via
   `montecarlo.combine_pert_leaves` (independent PERT sum + the same skewed AI-reduction sampler
   the twins use), and attribute role hours from the leaves' EXPLICIT role assignments (not the
   percentage-based `attribute_roles` — the user assigned a role per leaf).
3. Feed those `PhaseEstimate`s straight into `commercial_processing` + `synthesize_estimate`
   (plain async functions over an `EstimationState` dict) to get a fully-costed, staffed,
   contingency-adjusted `DualScenarioEstimate` — identical to the twin tail, zero duplication.

Load-bearing invariants preserved (same as the twins): `most_likely` is Σ leaf modes;
`ai.most_likely == manual.most_likely × (1 − eff)`; `Σ role_hours == most_likely`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from contingency_admin import resolve_contingency_pct
from db.repositories import get_reduction_bands
from models.project_schema import RoleRoster, Stage2Context, Stage3Context
from models.twin_outputs import (
    DualScenarioEstimate,
    Phase,
    PhaseEstimate,
    RoleHours,
)
from models.wbs_schema import WBS_DEFAULT_CONTINGENCY_PCT, WbsCalculateRequest
from models.wbs_task import WbsTaskInput, count_tasks, iter_leaves
from orchestrator.ai_acceleration import effective_ai_reduction
from orchestrator.montecarlo import combine_pert_leaves, make_rng, result_to_hour_range
from orchestrator.nodes._twin_base import make_reduction_sampler, tooling_for
from orchestrator.nodes.commercial_processing import compute_total_costs
from orchestrator.nodes.synthesize_estimate import synthesize_from_phase_estimates
from orchestrator.role_attribution import default_role_id

logger = logging.getLogger(__name__)

_WBS_TWIN_NAME = "WBS"
_WBS_ALGORITHM = "WBS bottom-up (PERT)"
# Bottom-up estimates carry a moderate, fixed self-confidence (no algorithmic confidence signal
# like the twins emit); the Monte Carlo band is what conveys the real uncertainty.
_WBS_CONFIDENCE = 0.6


async def _load_reduction_bands() -> dict:
    """DB-tunable reduction guardrail bands; {} (→ code defaults) when Postgres is off/unreachable."""
    try:
        return await get_reduction_bands()
    except Exception as exc:  # noqa: BLE001
        logger.warning("WBS: reduction-band fetch failed (%s); using code defaults", exc)
        return {}


def _remap_unknown_roles(
    grouped: dict[Phase, list[WbsTaskInput]], roster: RoleRoster
) -> dict[Phase, list[WbsTaskInput]]:
    """Snap any leaf ``role_id`` that isn't in the roster onto a default roster role.

    A WBS submitted via ``WbsCalculateRequest`` is NOT validated against the roster (the model has
    no roster context), so a hand-edited / stale tree can reference a role_id the roster no longer
    has. Left as-is, those hours land in a synthetic ``OTHER`` ``RoleHours`` entry that the
    downstream ``synthesize_estimate`` (which iterates only ``roster.roles``) silently drops from
    the headcount table, weekly burn, and cost — understating both. Remapping keeps every leaf's
    hours costed and staffed against a real role. Returns a new grouping (the input request's
    leaves are copied, never mutated in place)."""
    valid = {r.role_id for r in roster.roles}
    fallback = default_role_id(roster)
    return {
        phase: [
            leaf if leaf.role_id in valid else leaf.model_copy(update={"role_id": fallback})
            for leaf in leaves
        ]
        for phase, leaves in grouped.items()
    }


def _role_hours_for_phase(
    leaves: list[WbsTaskInput], roster: RoleRoster, *, scale: float
) -> list[RoleHours]:
    """Per-role hours from the leaves' explicit role assignments, scaled (1.0 manual, (1−eff) AI).

    Emits EXACTLY one `RoleHours` per roster role (zero-filled), matching the twins' convention.

    Any leaf whose ``role_id`` is not a roster role — including ``None`` / empty / whitespace, which
    ``_remap_unknown_roles`` is normally expected to have already snapped — is folded into the
    fallback roster role *here* rather than emitted as a synthetic ``OTHER`` row. A synthetic row
    would be silently dropped by ``synthesize_estimate`` (it iterates only ``roster.roles``),
    understating cost AND breaking the ``Σ role_hours == most_likely`` invariant. Folding keeps
    every leaf's hours attributed to a real, costed roster role even if a caller bypasses the remap,
    so this function is self-consistent standalone. (This defensive fold and the whole-tree
    ``_remap_unknown_roles`` pass intentionally back each other up: the remap is the normal path,
    this fold is the standalone safety net — keep both.)"""
    valid = {r.role_id for r in roster.roles}
    fallback = default_role_id(roster)
    by_role: dict[str, float] = defaultdict(float)
    for leaf in leaves:
        rid = leaf.role_id if (leaf.role_id and leaf.role_id in valid) else fallback
        by_role[rid] += leaf.most_likely or 0.0
    return [
        RoleHours(
            role_id=r.role_id,
            role_description=r.description,
            category=r.category,
            seniority=r.seniority,
            hours=by_role.get(r.role_id, 0.0) * scale,
        )
        for r in roster.roles
    ]


def _phase_estimate(
    phase: Phase,
    leaves: list[WbsTaskInput],
    *,
    roster: RoleRoster,
    stage3: Stage3Context,
    regulated: bool,
    reduction_bands: dict,
    estimate_id: str,
) -> PhaseEstimate:
    """Roll one phase's leaves into a PhaseEstimate via the shared MC + reduction machinery."""
    reduction_ctx: dict[str, Any] = {
        "phase": phase,
        "codebase": stage3.codebase_context,
        "tooling": tooling_for(stage3, phase),
        "roster": roster,
        "regulated": regulated,
        "bands": reduction_bands,
    }
    # WBS proposes no LLM reduction (like Discovery/UX): use the guardrail band midpoint.
    eff = effective_ai_reduction(proposed_reduction=None, **reduction_ctx)
    sampler = make_reduction_sampler(
        reduction_ctx=reduction_ctx, proposed_point=None, reduction_range=None
    )
    rng = make_rng(f"{estimate_id}:{phase.value}:wbs")
    bands = [
        (leaf.optimistic or 0.0, leaf.most_likely or 0.0, leaf.pessimistic or 0.0)
        for leaf in leaves
    ]
    manual_mc, ai_mc = combine_pert_leaves(
        bands, reduction_sampler=sampler, eff_point=eff, rng=rng
    )
    return PhaseEstimate(
        phase=phase,
        twin_name=_WBS_TWIN_NAME,
        algorithm=_WBS_ALGORITHM,
        ai_assisted_hours=result_to_hour_range(ai_mc),
        manual_only_hours=result_to_hour_range(manual_mc),
        ai_assisted_role_hours=_role_hours_for_phase(leaves, roster, scale=1.0 - eff),
        manual_only_role_hours=_role_hours_for_phase(leaves, roster, scale=1.0),
        confidence=_WBS_CONFIDENCE,
        breakdown={"leaf_count": float(len(leaves))},
        effective_ai_reduction_pct=round(eff * 100, 1),
        notes=f"Bottom-up rollup of {len(leaves)} WBS leaf task(s) in this phase.",
    )


def _group_leaves_by_phase(tree: list[WbsTaskInput]) -> dict[Phase, list[WbsTaskInput]]:
    grouped: dict[Phase, list[WbsTaskInput]] = defaultdict(list)
    for leaf in iter_leaves(tree):
        if leaf.phase is not None:
            grouped[leaf.phase].append(leaf)
    return grouped


async def build_wbs_estimate(
    req: WbsCalculateRequest, *, estimate_id: str
) -> DualScenarioEstimate:
    """Roll a WBS tree up into a DualScenarioEstimate (no persistence — preview + commit share this)."""
    stage2 = req.stage2 or Stage2Context()
    roster = stage2.roster if stage2.roster.roles else RoleRoster.default()
    if roster is not stage2.roster:
        stage2 = stage2.model_copy(update={"roster": roster})
    stage3 = req.stage3 or Stage3Context()
    regulated = bool(stage2.regulatory_requirements)

    reduction_bands = await _load_reduction_bands()
    # The WBS flow carries its OWN explicit contingency (the request field, default 30%) — it does
    # NOT read the global app_settings contingency the parametric/quick estimate uses.
    raw_contingency = (
        req.contingency_pct if req.contingency_pct is not None else WBS_DEFAULT_CONTINGENCY_PCT
    )
    contingency_pct = resolve_contingency_pct(raw_contingency)

    grouped = _remap_unknown_roles(_group_leaves_by_phase(req.tree), roster)
    # Iterate Phase (not dict) so phases stay in canonical order.
    phase_estimates = [
        _phase_estimate(
            phase,
            grouped[phase],
            roster=roster,
            stage3=stage3,
            regulated=regulated,
            reduction_bands=reduction_bands,
            estimate_id=estimate_id,
        )
        for phase in Phase
        if grouped.get(phase)
    ]

    # Reuse the twin tail through its typed seams: base labor cost, then variance-combine +
    # Brooks staffing + contingency + headcount — no untyped EstimationState dict, no type: ignore.
    total_ai_cost, total_manual_cost = compute_total_costs(phase_estimates, roster)
    final = await synthesize_from_phase_estimates(
        phase_estimates,
        stage2=stage2,
        total_cost_ai_assisted_usd=total_ai_cost,
        total_cost_manual_only_usd=total_manual_cost,
        contingency_pct=contingency_pct,
    )
    logger.info(
        "WBS rollup complete: %d task(s) across %d phase(s); ai_ml=%.0fh manual_ml=%.0fh",
        count_tasks(req.tree),
        len(phase_estimates),
        final.total_ai_assisted_hours.most_likely,
        final.total_manual_only_hours.most_likely,
    )
    return final

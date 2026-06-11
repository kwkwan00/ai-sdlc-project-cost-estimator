"""Shared role attribution per planning outline §7 "Role Attribution Logic".

Every twin produces total hours, then calls `attribute_roles()` to split them across
the user-defined roster. Each phase applies caps/overrides on top of the user's
percentages, keyed on role tags rather than fixed role names — so a custom roster
like {"Tech Lead": engineering/senior, "QA Engineer": qa/mid, "DevOps": devops/senior}
still gets sensible phase biases.

Phase overrides (generalized from the 4-role version):

- DISCOVERY        — senior-biased. Cap each junior-tagged role at 25%; push excess to
                     the same-category senior (fall back to any senior).
- UX_DESIGN        — product/design-biased. Ensure (product + ui_ux) categories total
                     >= 40%; pull shortfall from other categories proportionally.
- CODE_REVIEW      — strongly senior-biased. Cap each junior-tagged role at 15%; push
                     excess to the same-category senior.
- DEPLOYMENT       — technical-biased. Ensure (engineering + devops + data) categories
                     total >= 75%; pull shortfall from product/ui_ux/qa/other.
- DEVELOPMENT / QA_TESTING — honor user input as-is.

All overrides are re-normalized so the returned hours sum to `total_hours` (within
float tolerance). If the roster can't satisfy an override (e.g. no senior exists to
absorb excess), the capped slice stays at the cap and re-normalization redistributes
the missing mass across remaining roles.
"""

from __future__ import annotations

import logging

from models.project_schema import CustomRole, RoleRoster
from models.twin_outputs import Phase, RoleCategory, RoleHours, RoleSeniority

logger = logging.getLogger(__name__)

TECH_CATEGORIES = {RoleCategory.ENGINEERING, RoleCategory.DEVOPS, RoleCategory.DATA}
PRODUCT_DESIGN_CATEGORIES = {RoleCategory.PRODUCT, RoleCategory.UI_UX}


def _normalize(pcts: dict[str, float]) -> dict[str, float]:
    total = sum(pcts.values())
    if total <= 0:
        if not pcts:
            return {}
        share = 1.0 / len(pcts)
        return {k: share for k in pcts}
    return {k: v / total for k, v in pcts.items()}


def _push_junior_excess_to_senior(
    p: dict[str, float], roster: RoleRoster, cap: float
) -> dict[str, float]:
    """Cap each junior-seniority role at `cap`; redistribute excess to seniors."""
    seniors = [r for r in roster.roles if r.seniority == RoleSeniority.SENIOR]
    for r in roster.roles:
        if r.seniority != RoleSeniority.JUNIOR:
            continue
        current = p.get(r.role_id, 0.0)
        if current <= cap:
            continue
        excess = current - cap
        p[r.role_id] = cap
        logger.debug(
            "role_attribution: capped junior role %r from %.2f to %.2f (cap)",
            r.role_id,
            current,
            cap,
        )
        # Prefer a same-category senior so the discipline stays consistent.
        same_cat_senior = next(
            (s for s in seniors if s.category == r.category), None
        )
        if same_cat_senior is not None:
            p[same_cat_senior.role_id] += excess
        elif seniors:
            share = excess / len(seniors)
            for s in seniors:
                p[s.role_id] += share
        # else: no seniors at all — excess is lost, _normalize redistributes.
    return p


def _ensure_category_floor(
    p: dict[str, float],
    roster: RoleRoster,
    target_categories: set[RoleCategory],
    floor: float,
    *,
    preferred_recipients: list[RoleCategory] | None = None,
) -> dict[str, float]:
    """Ensure target categories together hold at least `floor` of the total share.

    Shortfall is pulled from non-target categories proportionally, then added to
    `preferred_recipients` categories first (so e.g. UX_DESIGN floors push to UI_UX
    before PRODUCT). Falls through to any target-category role if no preferred
    recipient exists.
    """
    target_total = sum(
        p.get(r.role_id, 0.0)
        for r in roster.roles
        if r.category in target_categories
    )
    if target_total >= floor:
        return p

    shortfall = floor - target_total
    non_target = [r for r in roster.roles if r.category not in target_categories]
    non_target_total = sum(p.get(r.role_id, 0.0) for r in non_target)
    if non_target_total > 0:
        for r in non_target:
            p[r.role_id] -= shortfall * (p[r.role_id] / non_target_total)

    # Add shortfall to preferred categories first.
    recipients_by_pref: list[CustomRole] = []
    for cat in preferred_recipients or []:
        recipients_by_pref.extend(r for r in roster.roles if r.category == cat)
    if not recipients_by_pref:
        recipients_by_pref = [r for r in roster.roles if r.category in target_categories]

    if recipients_by_pref:
        share = shortfall / len(recipients_by_pref)
        for r in recipients_by_pref:
            p[r.role_id] += share
    return p


def _apply_phase_overrides(
    base: dict[str, float], roster: RoleRoster, phase: Phase
) -> dict[str, float]:
    p = dict(base)

    if phase is Phase.DISCOVERY:
        _push_junior_excess_to_senior(p, roster, cap=0.25)
    elif phase is Phase.UX_DESIGN:
        _ensure_category_floor(
            p,
            roster,
            target_categories=PRODUCT_DESIGN_CATEGORIES,
            floor=0.40,
            preferred_recipients=[RoleCategory.UI_UX, RoleCategory.PRODUCT],
        )
    elif phase is Phase.CODE_REVIEW:
        _push_junior_excess_to_senior(p, roster, cap=0.15)
    elif phase is Phase.DEPLOYMENT:
        _ensure_category_floor(
            p,
            roster,
            target_categories=TECH_CATEGORIES,
            floor=0.75,
            preferred_recipients=[
                RoleCategory.DEVOPS,
                RoleCategory.ENGINEERING,
                RoleCategory.DATA,
            ],
        )
    # DEVELOPMENT and QA_TESTING honor user input as-is.

    return _normalize({k: max(v, 0.0) for k, v in p.items()})


def attribute_roles(
    total_hours: float,
    roster: RoleRoster,
    phase: Phase,
) -> list[RoleHours]:
    """Split `total_hours` across the user's roster, applying phase overrides.

    Returns one `RoleHours` entry per role in the roster (including 0-hour entries
    for roles fully zeroed out by overrides — keeps the output stable for the
    frontend's per-role cost table).
    """
    if not roster.roles:
        return []

    base = {r.role_id: r.percentage / 100.0 for r in roster.roles}
    base = _normalize(base)
    adjusted = _apply_phase_overrides(base, roster, phase)

    return [
        RoleHours(
            role_id=r.role_id,
            role_description=r.description,
            category=r.category,
            seniority=r.seniority,
            hours=total_hours * adjusted.get(r.role_id, 0.0),
        )
        for r in roster.roles
    ]

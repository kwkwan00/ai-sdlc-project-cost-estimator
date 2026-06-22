"""Team-roster proposal agent — runs after the prefill agent.

Given the interpreted project context (a `Stage2Context`), this agent sketches a
high-level delivery plan and proposes an initial team roster tailored to the
work. It mirrors the prefill agent's shape: a single forced-tool-use Claude call
(`call_structured`) against an enum-constrained response model, followed by a
deterministic backstop that turns the proposal into a valid `RoleRoster` the
Stage 2 wizard renders directly.

Division of labor, by design:
- **LLM (Sonnet, fast)** proposes the *structure*: which roles, their category /
  seniority tags, a rough effort split, and a short staffing rationale.
- **Deterministic backstop** owns everything that must be exact or is a business
  input: stable unique `role_id`s, hourly rates (from a controlled table — never
  LLM-proposed, since rates flow into cost math), and rebalancing percentages to
  sum to exactly 100 so `RoleRoster`'s validator always passes.

Kept free of any `prefill` import so `prefill` can import from here without a
cycle (the agent takes a `Stage2Context`, not prefill's `NormalizedProjectContext`).
"""

from __future__ import annotations

import logging
import math
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import get_settings
from models.project_schema import CustomRole, RoleRoster, Stage2Context
from models.twin_outputs import RoleCategory, RoleSeniority
from orchestrator.llm import call_structured, render_context_block
from orchestrator.nodes._twin_base import load_prompt
from pricing import resolve_rate
from slug import unique_slug

logger = logging.getLogger(__name__)

_MAX_ROLES = 8


# ---------- agent response model (enum-constrained; LLM emits structure only) ----------


class ProjectPlanItem(BaseModel):
    """One high-level workstream in the proposed delivery plan."""

    model_config = ConfigDict(extra="forbid")
    workstream: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=160)


class ProposedRole(BaseModel):
    """A role the agent proposes. No role_id / rate — those are assigned by the
    deterministic backstop, not the LLM.

    ``catalog_role_id`` is the agent's optional, EXPLICIT selection of an admin predefined
    rate-card role (by its id, listed in the prompt) — when it's a valid catalog id the backstop
    uses that role's exact rate and carries its id into the roster (so identity + pricing are a
    deterministic key lookup, not a fuzzy label match). Null / unknown → priced from the grid."""

    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=120)
    category: RoleCategory = RoleCategory.OTHER
    seniority: RoleSeniority = RoleSeniority.OTHER
    percentage: float = Field(default=0.0, ge=0, le=100)
    catalog_role_id: str | None = Field(default=None, max_length=64)


class RosterProposal(BaseModel):
    """Proposed high-level plan + team roster for the Stage 2 wizard.

    Kept compact on purpose: this agent runs in the prefill request's critical
    path while the user waits, and output volume dominates its latency.
    """

    model_config = ConfigDict(extra="forbid")
    project_plan: list[ProjectPlanItem] = Field(default_factory=list, max_length=6)
    staffing_rationale: str = Field(default="", max_length=300)
    roles: list[ProposedRole] = Field(min_length=1, max_length=_MAX_ROLES)


# ---------- deterministic backstop ----------


def _unique_ids(bases: list[str]) -> list[str]:
    """Assign unique role_ids (<=64 chars) from the given per-role base strings, suffixing on
    collision (shared `unique_slug` keeps the truncation rule consistent with the rate-card admin's
    id minting)."""
    seen: set[str] = set()
    return [unique_slug(base, seen) for base in bases]


def _make_unique_ids(roles: list[ProposedRole]) -> list[str]:
    """Unique role_ids from the `{seniority}_{category}` tags (the grid-priced path)."""
    return _unique_ids(
        [f"{role.seniority.value}_{role.category.value}" for role in roles]
    )


def _rebalance_to_100(weights: list[float]) -> list[int]:
    """Turn rough float effort weights into whole-integer percentages summing to
    exactly 100 (Hamilton largest-remainder), with no 0% rows.

    Clamp negatives, fall back to an equal split if everything is zero/negative,
    renormalize, floor, then hand the leftover points to the largest fractional
    parts (ties broken by lower index). Finally shift points off the largest
    role onto any 0% rows — feasible because the caller caps to <=8 roles.
    """
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [100]

    clamped = [max(w, 0.0) for w in weights]
    total = sum(clamped)
    if total <= 0:
        clamped = [1.0] * n
        total = float(n)

    raw = [w / total * 100.0 for w in clamped]
    floors = [int(math.floor(x)) for x in raw]
    remainder = 100 - sum(floors)  # 0..n

    by_frac = sorted(range(n), key=lambda i: (-(raw[i] - math.floor(raw[i])), i))
    result = floors[:]
    for k in range(remainder):
        result[by_frac[k]] += 1

    # Eliminate 0% rows by moving a point from the current largest entry.
    while any(r == 0 for r in result):
        recv = max((i for i in range(n) if result[i] == 0), key=lambda i: (raw[i], -i))
        donor = max(range(n), key=lambda i: (result[i], -i))
        if result[donor] <= 1:
            break  # cannot fix without creating a new zero; leave as-is
        result[donor] -= 1
        result[recv] += 1

    return result


class CatalogRole(NamedTuple):
    """One admin predefined rate-card role, as offered to the roster agent. ``role_id`` is the
    stable selection key (the agent sets ``ProposedRole.catalog_role_id`` to it)."""

    role_id: str
    label: str
    category: str
    seniority: str
    rate: float


def proposal_to_roster(
    proposal: RosterProposal,
    rate_overrides: dict[tuple[RoleCategory, RoleSeniority], float] | None = None,
    catalog: list[CatalogRole] | None = None,
) -> RoleRoster:
    """Map the agent's proposal into a valid RoleRoster (the wizard's shape).

    Caps role count, assigns unique ids + table rates, and rebalances
    percentages to sum to 100 so `RoleRoster`'s validator passes. Falls back to
    `RoleRoster.default()` if there's nothing usable or validation somehow fails.

    ``catalog`` lets the roster draw on the admin's custom rate-card roles by **explicit selection**:
    when a proposed role's ``catalog_role_id`` is a valid catalog id, that role's EXACT rate is used
    (instead of the ``(category, seniority)`` grid rate) and the catalog id is carried into the
    roster as the role_id, so identity + pricing are a deterministic key lookup. An absent/unknown
    ``catalog_role_id`` → grid pricing + a tag-derived id (no silent fuzzy matching).
    """
    roles = list(proposal.roles)
    if not roles:
        return RoleRoster.default()

    # Cap to _MAX_ROLES, keeping the highest-percentage roles (stable on ties).
    if len(roles) > _MAX_ROLES:
        keep = set(sorted(range(len(roles)), key=lambda i: (-roles[i].percentage, i))[:_MAX_ROLES])
        roles = [r for i, r in enumerate(roles) if i in keep]

    catalog_by_id = {c.role_id: c for c in (catalog or [])}

    def _resolve(role: ProposedRole) -> tuple[float, str]:
        """(rate, id_base) — the selected catalog role's exact rate + its id when the agent picked a
        valid one; otherwise the grid rate + a tag-derived base."""
        sel = catalog_by_id.get(role.catalog_role_id) if role.catalog_role_id else None
        if sel is not None:
            return sel.rate, sel.role_id
        return (
            resolve_rate(role.category, role.seniority, rate_overrides),
            f"{role.seniority.value}_{role.category.value}",
        )

    resolved = [_resolve(role) for role in roles]
    ids = _unique_ids([base for _, base in resolved])
    pcts = _rebalance_to_100([r.percentage for r in roles])
    custom = [
        CustomRole(
            role_id=rid,
            description=role.description,
            category=role.category,
            seniority=role.seniority,
            rate_per_hour=rate,
            percentage=float(pct),
        )
        for rid, role, (rate, _), pct in zip(ids, roles, resolved, pcts, strict=True)
    ]
    try:
        roster = RoleRoster(roles=custom)
    except ValidationError as exc:  # pragma: no cover - belt-and-suspenders
        logger.warning("proposed roster failed validation (%s); using default", exc)
        return RoleRoster.default()
    logger.info(
        "roster proposal mapped to %d role(s); categories=%s",
        len(custom),
        sorted({r.category.value for r in custom}),
    )
    return roster


# ---------- agent entry point ----------


def _custom_role_catalog_block(catalog: list[CatalogRole]) -> str:
    """Render the admin's predefined custom roles as a prompt block instructing the agent to SELECT
    one by id (``catalog_role_id``) when a role it would propose corresponds to it, so the org's set
    rate applies. Empty list → "" (no block)."""
    if not catalog:
        return ""
    lines = "\n".join(
        f"- id={c.role_id} · {c.label} ({c.category}/{c.seniority}) · ${c.rate:.0f}/h"
        for c in catalog
    )
    return (
        "Your organization has these predefined roles with set hourly rates. When a role you would "
        "propose corresponds to one of them, set that role's `catalog_role_id` to the matching id "
        "below (you may still give it a natural `description`/`category`/`seniority`). Select one "
        "ONLY when it genuinely fits; otherwise leave `catalog_role_id` null and propose normally."
        f"\n{lines}\n\n"
    )


async def run_roster_agent(
    stage2: Stage2Context,
    raw_input: str,
    custom_roles: list[CatalogRole] | None = None,
) -> RosterProposal:
    """Run the roster agent: one fast Sonnet forced-tool-use call.

    ``custom_roles`` is the admin's rate-card catalog (``CatalogRole`` per predefined role); when
    present it's injected into the prompt so the agent can deliberately SELECT a predefined role by
    its id (``ProposedRole.catalog_role_id``), which the backstop then prices at the org's rate.

    Raises on LLM failure (no API key, network); the caller in `prefill` handles
    the fallback to the default roster.
    """
    system = load_prompt("roster_agent")
    context = {
        "industry": stage2.industry,
        "project_type": stage2.project_type.value,
        "screen_count_estimate": stage2.screen_count_estimate,
        "integration_count": stage2.integration_count,
        "integration_list": stage2.integration_list,
        "regulatory_requirements": stage2.regulatory_requirements,
    }
    user = (
        f"Project description:\n\n{raw_input}\n\n"
        f"Interpreted project context:\n{render_context_block(context)}\n\n"
        f"{_custom_role_catalog_block(custom_roles or [])}"
        "Sketch the high-level delivery plan, then propose the team roster."
    )
    logger.debug(
        "running roster agent (model=%s, industry=%s, project_type=%s)",
        get_settings().anthropic_model_roster,
        stage2.industry or "unknown",
        stage2.project_type.value,
    )
    return await call_structured(
        system=system,
        user=user,
        response_model=RosterProposal,
        tool_name="propose_team_roster",
        model=get_settings().anthropic_model_roster,
        effort="low",
    )

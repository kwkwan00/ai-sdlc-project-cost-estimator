"""WBS planner agent — drafts a Work Breakdown Structure from a project description.

Mirrors the other non-twin agents (`prefill.py`, `roster_agent.py`): one forced-tool-use
Claude call (the same `call_structured` plumbing the twins use) against a Pydantic response
model, pinned to its own model tier, with a deterministic fallback so the endpoint always
returns an editable tree even with no API key.

The LLM emits a flat **two-level** structure (work packages → leaf tasks) — a non-recursive
tool schema that's easy for the model and avoids `$ref` self-references. A deterministic
backstop (`_planner_to_tree`) converts it to the canonical `WbsTaskInput` tree: it assigns
stable ids, coerces each leaf's phase to a valid `Phase`, snaps each `role_id` to a real roster
role (falling back to a sensible default), and lets `WbsTaskInput` coerce the 3-point ordering.
So the editor never receives an invalid tree.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from pydantic import BaseModel, ConfigDict, Field

from config import get_settings
from models.project_schema import (
    CodebaseContext,
    ProjectType,
    RoleRoster,
    Stage2Context,
    Stage3Context,
)
from models.twin_outputs import Phase, RoleCategory
from models.wbs_schema import WbsDraftRequest
from models.wbs_task import WbsTaskInput
from orchestrator.llm import call_structured, render_context_block
from orchestrator.nodes._twin_base import load_prompt
from orchestrator.nodes.parse_input import ParsedContext, extract_context_from_raw
from orchestrator.role_attribution import default_role_id

logger = logging.getLogger(__name__)

# Nominal per-phase fallback skeleton: (phase, preferred role category, 3-point hours). Used
# when the LLM is unavailable so the editor always opens with a full-lifecycle starting point.
_FALLBACK_SKELETON: list[tuple[Phase, RoleCategory, tuple[float, float, float]]] = [
    (Phase.DISCOVERY, RoleCategory.PRODUCT, (8, 16, 32)),
    (Phase.UX_DESIGN, RoleCategory.UI_UX, (8, 16, 32)),
    (Phase.DEVELOPMENT, RoleCategory.ENGINEERING, (40, 80, 160)),
    (Phase.CODE_REVIEW, RoleCategory.ENGINEERING, (8, 16, 24)),
    (Phase.DEPLOYMENT, RoleCategory.DEVOPS, (8, 16, 32)),
    (Phase.QA_TESTING, RoleCategory.QA, (16, 24, 40)),
]


class WbsPlannerLeaf(BaseModel):
    """One estimable leaf task the planner proposes (lenient — backstopped on the way out)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    phase: str = Field(default="", description="discovery|ux_design|development|code_review|deployment|qa_testing")
    role_id: str = Field(default="", description="A role_id from the roster")
    optimistic: float = Field(default=0.0, ge=0)
    most_likely: float = Field(default=0.0, ge=0)
    pessimistic: float = Field(default=0.0, ge=0)


class WbsPlannerPackage(BaseModel):
    """A work package grouping leaf tasks (one level of hierarchy)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    tasks: list[WbsPlannerLeaf] = Field(default_factory=list)


class WbsPlannerResponse(BaseModel):
    """The planner agent's forced-tool-use output: a two-level WBS + brief notes."""

    model_config = ConfigDict(extra="forbid")
    packages: list[WbsPlannerPackage] = Field(default_factory=list)
    notes: str = Field(default="", max_length=2000)


def _new_id() -> str:
    return uuid.uuid4().hex


def _coerce_phase(hint: str, default: Phase = Phase.DEVELOPMENT) -> Phase:
    try:
        return Phase(hint.strip().lower())
    except (ValueError, AttributeError):
        return default


def _role_for_category(roster: RoleRoster, category: RoleCategory, fallback: str) -> str:
    """The best roster role for a phase's natural category, else the default role."""
    for r in roster.roles:
        if r.category == category:
            return r.role_id
    return fallback


def _resolve_role(role_id: str, roster: RoleRoster, fallback: str) -> str:
    valid = {r.role_id for r in roster.roles}
    return role_id if role_id in valid else fallback


# --- complexity-aware bottom-up realism factor --------------------------------------------
#
# Bottom-up LLM estimates are systematically optimistic, and the optimism grows with hidden
# complexity (compliance, integration friction, brownfield navigation, sheer surface area). We
# derive the correction from the project signals already collected on Stage 2/3 rather than a flat
# constant. `Settings.wbs_effort_scale` globally tunes the result; the factor is clamped to a sane
# band so a degenerate input can't zero or explode the estimate.
_EFFORT_BASE = 1.5
_EFFORT_MIN = 1.2
_EFFORT_MAX = 3.0
_CODEBASE_BUMP: dict[CodebaseContext, float] = {
    CodebaseContext.GREENFIELD: 0.0,
    CodebaseContext.BROWNFIELD_SMALL: 0.10,
    CodebaseContext.BROWNFIELD_LARGE_FAMILIAR: 0.20,
    CodebaseContext.BROWNFIELD_LARGE_UNFAMILIAR: 0.35,
}
_PROJECT_TYPE_BUMP: dict[ProjectType, float] = {
    ProjectType.GREENFIELD: 0.0,
    ProjectType.ENHANCEMENT: 0.0,
    ProjectType.INTEGRATION: 0.10,
    ProjectType.LEGACY_REPLACEMENT: 0.15,
    ProjectType.DATA_MIGRATION: 0.15,
    ProjectType.AI_ML_BUILD: 0.15,
}


def _coerce_project_type(hint: str) -> ProjectType:
    try:
        return ProjectType(hint.strip().lower())
    except (ValueError, AttributeError):
        return ProjectType.GREENFIELD


def _effective_stage2_for_factor(
    stage2: Stage2Context | None, parsed: ParsedContext
) -> Stage2Context:
    """Fill the complexity signals the WBS wizard does NOT collect (project type, integrations,
    screens, regulatory regimes) from the LLM-parsed description, so ``_complexity_effort_factor``
    isn't inert on the real flow (the wizard only captures roster + codebase). An explicit
    non-default value already on ``stage2`` (a future richer wizard) takes precedence over the
    inferred one."""
    base = stage2 or Stage2Context()
    project_type = (
        base.project_type
        if base.project_type != ProjectType.GREENFIELD
        else _coerce_project_type(parsed.project_type_hint)
    )
    return base.model_copy(
        update={
            "project_type": project_type,
            "integration_count": base.integration_count or len(parsed.integration_mentions),
            "screen_count_estimate": base.screen_count_estimate or (parsed.screen_count_estimate or None),
            "regulatory_requirements": base.regulatory_requirements
            or list(dict.fromkeys(parsed.regulatory_mentions)),
        }
    )


def _complexity_effort_factor(
    stage2: Stage2Context | None, stage3: Stage3Context | None, *, scale: float = 1.0
) -> float:
    """Project-specific realism factor for the LLM-drafted bottom-up hours.

    Higher when the project carries more hidden work the LLM under-counts: regulated regimes
    (+0.15 each, ≤+0.45), integrations (+0.05 each, ≤+0.40), surface area (+0.1 per ~25 screens,
    ≤+0.30), heavier project types, and unfamiliar/large brownfield codebases. Multiplied by the
    global ``scale`` knob, then clamped to ``[_EFFORT_MIN, _EFFORT_MAX]``. (AI tooling is NOT folded
    in — it accelerates delivery, which the rollup applies separately as the AI-assisted scenario;
    the manual bottom-up estimate is optimistic regardless of tooling.)"""
    factor = _EFFORT_BASE
    if stage2 is not None:
        factor += min(0.45, 0.15 * len(stage2.regulatory_requirements))
        factor += min(0.40, 0.05 * max(0, stage2.integration_count))
        screens = stage2.screen_count_estimate or 0
        factor += min(0.30, 0.10 * (screens / 25.0))
        factor += _PROJECT_TYPE_BUMP.get(stage2.project_type, 0.0)
    if stage3 is not None:
        factor += _CODEBASE_BUMP.get(stage3.codebase_context, 0.0)
    factor *= scale if scale > 0 else 1.0
    return max(_EFFORT_MIN, min(_EFFORT_MAX, factor))


def _planner_to_tree(
    resp: WbsPlannerResponse, roster: RoleRoster, *, effort_multiplier: float = 1.0
) -> list[WbsTaskInput]:
    """Convert the lenient planner output into a validated `WbsTaskInput` tree (backstop).

    ``effort_multiplier`` is the already-computed complexity factor (see
    ``_complexity_effort_factor``) — NOT the raw setting — and scales the LLM-drafted hours to
    correct the systematic optimism of bottom-up task estimates; the global
    ``Settings.wbs_effort_scale`` tunes that computed factor. 1.0 leaves the hours as-is.
    """
    m = effort_multiplier if effort_multiplier > 0 else 1.0
    default_role = default_role_id(roster)
    tree: list[WbsTaskInput] = []
    for pkg in resp.packages:
        leaves: list[WbsTaskInput] = []
        for leaf in pkg.tasks:
            leaves.append(
                WbsTaskInput(
                    id=_new_id(),
                    name=leaf.name,
                    description=leaf.description,
                    phase=_coerce_phase(leaf.phase),
                    role_id=_resolve_role(leaf.role_id, roster, default_role),
                    # Round to 1 decimal (not whole hours): preserves sub-hour precision and avoids
                    # banker's-rounding a small task's bound down to 0 (e.g. round(0.5)==0).
                    optimistic=round(leaf.optimistic * m, 1),
                    most_likely=round(leaf.most_likely * m, 1),
                    pessimistic=round(leaf.pessimistic * m, 1),
                )
            )
        if not leaves:
            continue  # drop empty packages — a branch with no leaves can't be costed
        tree.append(
            WbsTaskInput(id=_new_id(), name=pkg.name, description=pkg.description, children=leaves)
        )
    return tree


def _fallback_tree(roster: RoleRoster) -> list[WbsTaskInput]:
    """Deterministic full-lifecycle skeleton (one package + leaf per phase). LLM-free."""
    default_role = default_role_id(roster)
    leaves: list[WbsTaskInput] = []
    for phase, category, (lo, ml, hi) in _FALLBACK_SKELETON:
        leaves.append(
            WbsTaskInput(
                id=_new_id(),
                name=f"{phase.value.replace('_', ' ').title()} work",
                description="Placeholder task — edit or replace.",
                phase=phase,
                role_id=_role_for_category(roster, category, default_role),
                optimistic=lo,
                most_likely=ml,
                pessimistic=hi,
            )
        )
    return [
        WbsTaskInput(
            id=_new_id(),
            name="Project work breakdown",
            description="Auto-generated starting point — refine the tasks, hours, and roles.",
            children=leaves,
        )
    ]


def _build_user_prompt(req: WbsDraftRequest, roster: RoleRoster, stage3: Stage3Context) -> str:
    context = {
        "project_description": req.raw_input,
        "roster": [
            {"role_id": r.role_id, "description": r.description, "category": r.category.value,
             "seniority": r.seniority.value}
            for r in roster.roles
        ],
        "phases": [p.value for p in Phase],
        "codebase_context": stage3.codebase_context.value,
        "ai_tooling": stage3.ai_tooling.model_dump(),
    }
    if req.stage2:
        context["industry"] = req.stage2.industry
        context["project_type"] = req.stage2.project_type.value
    return (
        "Draft a Work Breakdown Structure for this project.\n\n"
        f"{render_context_block({}, context)}\n\n"
        "Return work packages with leaf tasks via the tool. Assign each leaf a phase, a roster "
        "role_id, and a three-point hour estimate."
    )


async def run_wbs_planner(req: WbsDraftRequest) -> WbsPlannerResponse:
    """One forced-tool-use call returning the planner's two-level WBS. Raises on LLM failure."""
    roster = req.stage2.roster if req.stage2 and req.stage2.roster.roles else RoleRoster.default()
    stage3 = req.stage3 or Stage3Context()
    system = load_prompt("wbs_planner")
    user = _build_user_prompt(req, roster, stage3)
    logger.debug("running WBS planner (model=%s)", get_settings().anthropic_model_wbs)
    return await call_structured(
        system=system,
        user=user,
        response_model=WbsPlannerResponse,
        tool_name="propose_wbs",
        model=get_settings().anthropic_model_wbs,
        # A realistic WBS runs to 50–150+ leaf tasks; too low a cap truncates mid-`packages` (the
        # tool input then arrives with an empty list → we fall back to the generic skeleton). 16384
        # fits ~200 minimal-description leaves and stays under the non-streaming request ceiling
        # (above ~21k the SDK requires streaming for the potentially >10-minute call).
        max_tokens=16384,
    )


async def generate_wbs_tree(req: WbsDraftRequest) -> tuple[list[WbsTaskInput], str]:
    """Top-level entry point: draft a WBS tree (+ notes), degrading to a deterministic skeleton.

    Always returns a valid, leaf-complete `WbsTaskInput` tree — on any LLM failure (no API key,
    network, parse) it falls back to `_fallback_tree` so the editor always has something to edit.
    """
    roster = req.stage2.roster if req.stage2 and req.stage2.roster.roles else RoleRoster.default()
    try:
        # Draft the tree AND extract complexity signals from the description concurrently — the
        # WBS wizard collects neither industry/integrations/screens/regulatory, so the realism
        # factor would otherwise only ever see the codebase bump. extract_context_from_raw never
        # raises (it degrades internally), so it can't sink the planner result.
        resp, parsed = await asyncio.gather(
            run_wbs_planner(req), extract_context_from_raw(req.raw_input)
        )
        factor = _complexity_effort_factor(
            _effective_stage2_for_factor(req.stage2, parsed),
            req.stage3,
            scale=get_settings().wbs_effort_scale,
        )
        tree = _planner_to_tree(resp, roster, effort_multiplier=factor)
        if tree:
            logger.info("WBS planner drafted %d work package(s) (effort factor %.2f)", len(tree), factor)
            return tree, resp.notes
        logger.warning("WBS planner returned no usable packages; using fallback skeleton")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "WBS planner failed (%s); using fallback skeleton. Set ANTHROPIC_API_KEY for real drafts.",
            exc,
        )
    return _fallback_tree(roster), ""

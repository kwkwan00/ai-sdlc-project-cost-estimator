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
import json
import logging
import uuid
from collections.abc import Callable
from functools import cache
from pathlib import Path

import yaml
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
from orchestrator.llm import call_structured, render_context_block, stream_structured
from orchestrator.nodes.parse_input import ParsedContext, extract_context_from_raw
from orchestrator.prompts import load_prompt
from orchestrator.role_attribution import default_role_id

logger = logging.getLogger(__name__)

# The per-phase fallback skeleton (used when the LLM is unavailable so the editor always opens
# with a full-lifecycle starting point) is config, not code — it lives in the YAML below.
_FALLBACK_PATH = Path(__file__).parent.parent / "orchestrator" / "wbs" / "fallback_skeleton.yaml"


@cache
def _load_fallback_config() -> dict:
    """Load the deterministic WBS fallback skeleton from its YAML config. Degrades to an empty
    config (a 1-task safety net kicks in) rather than crashing a draft."""
    try:
        return yaml.safe_load(_FALLBACK_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("WBS fallback skeleton config unreadable (%s); using a minimal skeleton", exc)
        return {}


class WbsPlannerLeaf(BaseModel):
    """One estimable leaf task the planner proposes (lenient — backstopped on the way out)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    phase: str = Field(default="", description="discovery|ux_design|development|code_review|deployment|qa_testing")
    role_id: str = Field(default="", description="A role_id from the roster")
    # A short, stable handle the model assigns to THIS task so other tasks' `depends_on` can point at
    # it. The final tree uses real ids; the backstop translates these keys → ids (and drops unknowns).
    key: str = Field(default="", max_length=64, description="Short unique handle for this task (e.g. 'login-api')")
    depends_on: list[str] = Field(
        default_factory=list,
        description="keys of OTHER tasks that must finish before this one (task→task only)",
    )
    optimistic: float = Field(default=0.0, ge=0)
    most_likely: float = Field(default=0.0, ge=0)
    pessimistic: float = Field(default=0.0, ge=0)


class WbsPlannerPackage(BaseModel):
    """A work package grouping leaf tasks (one level of hierarchy)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    key: str = Field(default="", max_length=64, description="Short unique handle for this work package")
    depends_on: list[str] = Field(
        default_factory=list,
        description="keys of OTHER work packages that must finish before this one (package→package only)",
    )
    tasks: list[WbsPlannerLeaf] = Field(default_factory=list)


class WbsPlannerResponse(BaseModel):
    """The planner agent's forced-tool-use output: a two-level WBS + brief notes."""

    model_config = ConfigDict(extra="forbid")
    packages: list[WbsPlannerPackage] = Field(default_factory=list)
    notes: str = Field(default="", max_length=2000)


class _PackageNameStreamParser:
    """Incrementally extracts each work-package AND leaf-task ``name`` from the planner's streamed
    tool-input JSON, tagged by kind, so the UI can narrate what the planner is doing.

    The planner tool input is shaped ``{"packages": [{"name": ..., "tasks": [{"name": ...}]}], "notes": ...}``.
    A ``name`` key's string value is a *work package* at bracket-depth 3 and a *task* at depth 5 —
    depth 1 is the root object, 2 the ``packages`` array, 3 a package object, 4 its ``tasks`` array,
    5 a task object. This depth rule is coupled to ``WbsPlannerResponse`` (the only depth-2 array is
    ``packages``); revisit ``_PACKAGE_DEPTH`` / ``_TASK_DEPTH`` if that schema changes.

    A tiny character state machine, robust to a value being split across ``feed()`` calls (the SDK
    chunks the JSON arbitrarily). ``feed(chunk)`` returns the ``(kind, name)`` pairs — ``kind`` is
    ``"package"`` or ``"task"`` — completed within that chunk, in document order.
    """

    _PACKAGE_DEPTH = 3
    _TASK_DEPTH = 5

    def __init__(self) -> None:
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._buf: list[str] = []  # chars of the in-progress string literal
        self._last_key: str | None = None  # most recent object key awaiting its value
        self._value: str | None = None  # a just-closed string not yet consumed as a key or value

    @staticmethod
    def _decode(raw: str) -> str:
        """Decode JSON string escapes in a completed literal (the chars were captured verbatim)."""
        try:
            return json.loads('"' + raw + '"')
        except (ValueError, json.JSONDecodeError):
            return raw

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        for ch in chunk:
            if self._in_string:
                if self._escape:
                    self._buf.append(ch)
                    self._escape = False
                elif ch == "\\":
                    self._buf.append(ch)
                    self._escape = True
                elif ch == '"':
                    self._in_string = False
                    self._value = "".join(self._buf)
                    self._buf = []
                else:
                    self._buf.append(ch)
                continue
            if ch == '"':
                self._in_string = True
                self._buf = []
            elif ch == ":":
                # The string we just closed was an object key, not a value.
                self._last_key = self._value
                self._value = None
            elif ch == ",":
                self._maybe_emit(events)
                self._last_key = None
                self._value = None
            elif ch in "{[":
                self._depth += 1
            elif ch in "}]":
                self._maybe_emit(events)  # a value can terminate by closing its container
                self._depth -= 1
                self._last_key = None
                self._value = None
        return events

    def _maybe_emit(self, events: list[tuple[str, str]]) -> None:
        if self._last_key != "name" or self._value is None:
            return
        if self._depth == self._PACKAGE_DEPTH:
            events.append(("package", self._decode(self._value)))
        elif self._depth == self._TASK_DEPTH:
            events.append(("task", self._decode(self._value)))


def _new_id() -> str:
    return uuid.uuid4().hex


def _coerce_phase(hint: str, default: Phase = Phase.DEVELOPMENT) -> Phase:
    try:
        return Phase(hint.strip().lower())
    except (ValueError, AttributeError):
        return default


def _phase_in_scope(phase: Phase, selected_phases: list[Phase] | None) -> bool:
    """Whether a phase is in the engagement's scope. None / empty ⇒ everything is in scope."""
    return not selected_phases or phase in selected_phases


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
            # integration_count is a non-Optional int defaulting to 0, and the WBS wizard always sends
            # 0 (it doesn't collect integrations), so here 0 == "unset" → fall through to the inferred
            # count. (A richer future wizard that collects integrations would send a non-zero value.)
            "integration_count": base.integration_count or len(parsed.integration_mentions),
            # screen_count_estimate is Optional, so an explicit 0 ("zero screens") is distinct from None
            # ("unspecified"): only None falls through to the inferred count — a deliberate 0 wins
            # (don't use `or`, which would treat 0 as falsy and override the user's explicit input).
            "screen_count_estimate": (
                base.screen_count_estimate
                if base.screen_count_estimate is not None
                else (parsed.screen_count_estimate or None)
            ),
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
    resp: WbsPlannerResponse,
    roster: RoleRoster,
    *,
    effort_multiplier: float = 1.0,
    selected_phases: list[Phase] | None = None,
) -> list[WbsTaskInput]:
    """Convert the lenient planner output into a validated `WbsTaskInput` tree (backstop).

    ``effort_multiplier`` is the already-computed complexity factor (see
    ``_complexity_effort_factor``) — NOT the raw setting — and scales the LLM-drafted hours to
    correct the systematic optimism of bottom-up task estimates; the global
    ``Settings.wbs_effort_scale`` tunes that computed factor. 1.0 leaves the hours as-is.

    ``selected_phases`` scopes the tree: leaf tasks whose (coerced) phase is out of scope are
    dropped, and any work package left with no in-scope leaves is dropped too — so a disabled phase
    yields no work packages even if the LLM proposed some. Dependency edges onto a dropped node are
    pruned automatically (its key never enters the resolution maps). None / full set ⇒ no filtering.
    """
    m = effort_multiplier if effort_multiplier > 0 else 1.0
    default_role = default_role_id(roster)

    # Pass 1: assign real ids and record the model's key→id maps. Package keys live in one namespace;
    # task keys are tracked BOTH per-package (`local_leaf_keys`) and globally. Task-key resolution
    # prefers the SAME package, because LLM-invented slugs like "tests"/"setup"/"api" collide across
    # packages — a global-only first-wins map would silently route a package's `depends_on=["tests"]`
    # to some other package's tests. First definition of a key wins within each map.
    pkg_key_to_id: dict[str, str] = {}
    leaf_key_to_id: dict[str, str] = {}
    staged: list[tuple[WbsPlannerPackage, str, list[tuple[WbsPlannerLeaf, str]], dict[str, str]]] = []
    for pkg in resp.packages:
        # Drop out-of-scope leaves up front so their keys never become dependency targets and an
        # all-out-of-scope package falls away via the empty-package guard below.
        leaf_pairs = [
            (leaf, _new_id())
            for leaf in pkg.tasks
            if _phase_in_scope(_coerce_phase(leaf.phase), selected_phases)
        ]
        if not leaf_pairs:
            continue  # drop empty packages — a branch with no leaves can't be costed
        pkg_id = _new_id()
        local_leaf_keys: dict[str, str] = {}
        pkg_key = pkg.key.strip()
        if pkg_key and pkg_key not in pkg_key_to_id:
            pkg_key_to_id[pkg_key] = pkg_id
        for leaf, leaf_id in leaf_pairs:
            leaf_key = leaf.key.strip()
            if leaf_key:
                local_leaf_keys.setdefault(leaf_key, leaf_id)
                leaf_key_to_id.setdefault(leaf_key, leaf_id)
        staged.append((pkg, pkg_id, leaf_pairs, local_leaf_keys))

    def _resolve_deps(keys: list[str], own_id: str, *maps: dict[str, str]) -> list[str]:
        """Translate the model's dependency keys to real ids, trying each map in order (so a
        same-package map can shadow the global one), dropping unknown keys + self and de-duplicating
        while preserving order. (Cross-kind refs drop out: a key only appears in its kind's maps.)"""
        out: list[str] = []
        seen: set[str] = set()
        for key in keys:
            k = key.strip()
            dep_id = next((m[k] for m in maps if k in m), None)
            if dep_id and dep_id != own_id and dep_id not in seen:
                seen.add(dep_id)
                out.append(dep_id)
        return out

    # Pass 2: build the validated tree, resolving each node's depends_on against the right map(s).
    tree: list[WbsTaskInput] = []
    for pkg, pkg_id, leaf_pairs, local_leaf_keys in staged:
        leaves = [
            WbsTaskInput(
                id=leaf_id,
                name=leaf.name,
                description=leaf.description,
                phase=_coerce_phase(leaf.phase),
                role_id=_resolve_role(leaf.role_id, roster, default_role),
                # Round to 1 decimal (not whole hours): preserves sub-hour precision and avoids
                # banker's-rounding a small task's bound down to 0 (e.g. round(0.5)==0).
                optimistic=round(leaf.optimistic * m, 1),
                most_likely=round(leaf.most_likely * m, 1),
                pessimistic=round(leaf.pessimistic * m, 1),
                # same-package keys first, then the global map (cross-package references still work).
                depends_on=_resolve_deps(leaf.depends_on, leaf_id, local_leaf_keys, leaf_key_to_id),
            )
            for leaf, leaf_id in leaf_pairs
        ]
        tree.append(
            WbsTaskInput(
                id=pkg_id,
                name=pkg.name,
                description=pkg.description,
                children=leaves,
                depends_on=_resolve_deps(pkg.depends_on, pkg_id, pkg_key_to_id),
            )
        )
    return tree


def _fallback_tree(
    roster: RoleRoster, selected_phases: list[Phase] | None = None
) -> list[WbsTaskInput]:
    """Deterministic full-lifecycle skeleton (one package + leaf per phase), LLM-free. The task
    data + wording come from `orchestrator/wbs/fallback_skeleton.yaml`.

    ``selected_phases`` scopes the skeleton: tasks for out-of-scope phases are omitted (so a
    disabled phase yields no work even on the no-API-key path). None / full set ⇒ full lifecycle."""
    cfg = _load_fallback_config()
    default_role = default_role_id(roster)
    default_desc = str(cfg.get("default_task_description") or "Placeholder task — edit or replace.")
    leaves: list[WbsTaskInput] = []
    for t in cfg.get("tasks", []):
        phase = _coerce_phase(str(t.get("phase", "")))
        if not _phase_in_scope(phase, selected_phases):
            continue
        try:
            category = RoleCategory(str(t.get("category", "other")).strip().lower())
        except ValueError:
            category = RoleCategory.OTHER
        leaves.append(
            WbsTaskInput(
                id=_new_id(),
                name=str(t.get("name") or f"{phase.value.replace('_', ' ').title()} work"),
                description=str(t.get("description") or default_desc),
                phase=phase,
                role_id=_role_for_category(roster, category, default_role),
                optimistic=float(t.get("optimistic", 0)),
                most_likely=float(t.get("most_likely", 0)),
                pessimistic=float(t.get("pessimistic", 0)),
            )
        )
    if not leaves:  # config unreadable/empty — keep the draft openable with a minimal leaf
        # Honor scope even on this degenerate path: use a default phase that's actually in scope.
        minimal_phase = Phase.DEVELOPMENT
        if selected_phases and Phase.DEVELOPMENT not in selected_phases:
            minimal_phase = selected_phases[0]
        leaves = [
            WbsTaskInput(
                id=_new_id(),
                name=f"{minimal_phase.value.replace('_', ' ').title()} work",
                description=default_desc,
                phase=minimal_phase,
                role_id=default_role,
                optimistic=40,
                most_likely=80,
                pessimistic=160,
            )
        ]
    # Seed a simple linear dependency chain through the lifecycle (each task waits on the previous
    # one) so the skeleton demonstrates the "depends on" relationship even with no API key. The user
    # edits these like everything else.
    for prev, leaf in zip(leaves, leaves[1:], strict=False):
        leaf.depends_on = [prev.id]
    pkg = cfg.get("package") or {}
    return [
        WbsTaskInput(
            id=_new_id(),
            name=str(pkg.get("name") or "Project work breakdown"),
            description=str(pkg.get("description") or "Auto-generated starting point."),
            children=leaves,
        )
    ]


def _build_user_prompt(req: WbsDraftRequest, roster: RoleRoster, stage3: Stage3Context) -> str:
    # Phases the LLM may use. A strict subset scopes the draft — out-of-scope phases are removed
    # from the offered list AND called out explicitly below so the model doesn't draft for them
    # (the backstop also drops any it slips in).
    selected = set(req.selected_phases) if req.selected_phases else None
    phases_in_scope = [p for p in Phase if selected is None or p in selected]
    context = {
        "project_description": req.raw_input,
        "roster": [
            {"role_id": r.role_id, "description": r.description, "category": r.category.value,
             "seniority": r.seniority.value}
            for r in roster.roles
        ],
        "phases": [p.value for p in phases_in_scope],
        "codebase_context": stage3.codebase_context.value,
        "ai_tooling": stage3.ai_tooling.model_dump(),
    }
    if req.stage2:
        context["industry"] = req.stage2.industry
        context["project_type"] = req.stage2.project_type.value
    scope_note = ""
    if selected is not None and len(phases_in_scope) < len(list(Phase)):
        labels = ", ".join(p.value for p in phases_in_scope)
        scope_note = (
            f" SCOPE: this engagement covers ONLY these SDLC phases — {labels}. Do NOT create any "
            "work package or leaf task for a phase outside this set; those phases are out of scope."
        )
    return (
        "Draft a Work Breakdown Structure for this project.\n\n"
        f"{render_context_block({}, context)}\n\n"
        "Return work packages with leaf tasks via the tool. Assign each leaf a phase, a roster "
        "role_id, and a three-point hour estimate. Give every package and every task a short unique "
        "`key`, and use `depends_on` to list the keys of prerequisite nodes — packages depend only "
        "on other packages, tasks only on other tasks. Reference only keys you defined; keep the "
        f"dependencies acyclic.{scope_note}"
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


async def run_wbs_planner_streamed(
    req: WbsDraftRequest, *, on_node: Callable[[str, str], None]
) -> WbsPlannerResponse:
    """Streaming variant of `run_wbs_planner`: invokes ``on_node(kind, name)`` — ``kind`` is
    ``"package"`` or ``"task"`` — as each is drafted, then returns the same validated two-level WBS.
    Raises on LLM failure (the caller degrades to the deterministic skeleton)."""
    roster = req.stage2.roster if req.stage2 and req.stage2.roster.roles else RoleRoster.default()
    stage3 = req.stage3 or Stage3Context()
    system = load_prompt("wbs_planner")
    user = _build_user_prompt(req, roster, stage3)
    parser = _PackageNameStreamParser()

    def _on_delta(partial_json: str) -> None:
        for kind, name in parser.feed(partial_json):
            on_node(kind, name)

    logger.debug("streaming WBS planner (model=%s)", get_settings().anthropic_model_wbs)
    return await stream_structured(
        system=system,
        user=user,
        response_model=WbsPlannerResponse,
        tool_name="propose_wbs",
        model=get_settings().anthropic_model_wbs,
        max_tokens=16384,
        on_input_delta=_on_delta,
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
        tree = _planner_to_tree(
            resp, roster, effort_multiplier=factor, selected_phases=req.selected_phases
        )
        if tree:
            logger.info("WBS planner drafted %d work package(s) (effort factor %.2f)", len(tree), factor)
            return tree, resp.notes
        logger.warning("WBS planner returned no usable packages; using fallback skeleton")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "WBS planner failed (%s); using fallback skeleton. Set ANTHROPIC_API_KEY for real drafts.",
            exc,
        )
    return _fallback_tree(roster, req.selected_phases), ""


async def generate_wbs_tree_streamed(
    req: WbsDraftRequest, *, on_node: Callable[[str, str], None]
) -> tuple[list[WbsTaskInput], str]:
    """Streaming sibling of `generate_wbs_tree`: identical rollup + never-fail contract, but the
    planner runs in streaming mode and ``on_node(kind, name)`` fires for each work package / task as
    it is drafted. Always returns a valid tree — any LLM failure degrades to the non-streaming draft
    (and simply emits no node events)."""
    roster = req.stage2.roster if req.stage2 and req.stage2.roster.roles else RoleRoster.default()
    try:
        # Same concurrent draft + complexity-signal extraction as generate_wbs_tree, but the planner
        # streams its packages + tasks through on_node as they arrive.
        resp, parsed = await asyncio.gather(
            run_wbs_planner_streamed(req, on_node=on_node),
            extract_context_from_raw(req.raw_input),
        )
        factor = _complexity_effort_factor(
            _effective_stage2_for_factor(req.stage2, parsed),
            req.stage3,
            scale=get_settings().wbs_effort_scale,
        )
        tree = _planner_to_tree(
            resp, roster, effort_multiplier=factor, selected_phases=req.selected_phases
        )
        if tree:
            logger.info(
                "WBS planner (streamed) drafted %d work package(s) (effort factor %.2f)",
                len(tree), factor,
            )
            return tree, resp.notes
        logger.warning("WBS planner (streamed) returned no usable packages; using fallback skeleton")
        return _fallback_tree(roster, req.selected_phases), ""
    except Exception as exc:  # noqa: BLE001
        # Streaming has NO corrective retry (unlike call_structured). On a transient streaming failure
        # (e.g. a tool-input that fails validation) fall back to the NON-streaming draft, which retries
        # the planner once before degrading — so a one-off slip still yields a real planned tree rather
        # than the generic skeleton (which wouldn't match the package names that just streamed by).
        logger.warning("WBS planner stream failed (%s); retrying via the non-streaming draft path", exc)
        return await generate_wbs_tree(req)

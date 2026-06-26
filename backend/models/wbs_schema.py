"""Request / response models for the WBS (Work Breakdown Structure) flow.

The bottom-up sibling of the parametric twin flow: the user (seeded by an LLM draft)
builds a hierarchical task tree, attaches a three-point effort estimate + a role to
each leaf, and the backend rolls it up into the same ``DualScenarioEstimate`` the twins
produce. Drafts are resumable (persisted server-side in Neo4j) and duplicable.

``WbsTaskInput`` + the tree helpers live in ``models/wbs_task.py`` (a leaf module) so
``project_schema`` can reference the node type without a circular import.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.project_schema import Stage2Context, Stage3Context
from models.twin_outputs import LlmUsage, Phase
from models.wbs_task import MAX_WBS_NODES, WbsTaskInput

# Bottom-up WBS estimates are systematically optimistic (the complexity-aware effort factor only
# partly corrects this), so the WBS flow carries its OWN explicit contingency reserve defaulting to
# 30% — independent of the global ``app_settings`` contingency the parametric/quick estimate uses.
WBS_DEFAULT_CONTINGENCY_PCT = 30.0

# Cap nesting depth too (a WBS is realistically 2–4 levels). Bounds the recursive tree helpers
# (iter_leaves/count_tasks/flatten_tree/rebuild_tree) on user-submitted trees.
MAX_WBS_DEPTH = 8


def _assert_tree_bounds(tree: list[WbsTaskInput]) -> None:
    """Reject an over-large / over-deep user-submitted tree (DoS guard) BEFORE it reaches the
    expensive Monte Carlo rollup. Iterative walk so it adds no recursion of its own."""
    count = 0
    max_depth = 0
    stack: list[tuple[WbsTaskInput, int]] = [(n, 1) for n in tree]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_WBS_NODES:
            raise ValueError(f"WBS tree too large (> {MAX_WBS_NODES} nodes)")
        if depth > max_depth:
            max_depth = depth
        stack.extend((c, depth + 1) for c in node.children)
    if max_depth > MAX_WBS_DEPTH:
        raise ValueError(f"WBS tree too deeply nested (> {MAX_WBS_DEPTH} levels)")


def _assert_unique_ids(tree: list[WbsTaskInput]) -> None:
    """Reject a tree with duplicate node ids. The ``depends_on`` feature — the kind map in
    ``_sanitize_dependencies``, flatten/rebuild's ``parent_id`` keying, and the review schedule's
    finish-time map — all assume ids are globally unique; two nodes sharing one would mis-classify or
    conflate edges. The editor mints uuids, so this only trips on a hand-built / legacy / buggy
    client tree (a 422 there is far better than a silently wrong schedule). Iterative walk."""
    seen: set[str] = set()
    stack: list[WbsTaskInput] = list(tree)
    while stack:
        node = stack.pop()
        if node.id in seen:
            raise ValueError(f"WBS tree has a duplicate node id: {node.id!r}")
        seen.add(node.id)
        stack.extend(node.children)


def _sanitize_dependencies(tree: list[WbsTaskInput]) -> None:
    """Prune every ``depends_on`` reference that is dangling, self, or cross-kind — a work package
    may only depend on work packages, a task only on tasks. Mutates the tree in place.

    Lenient (prune, never 422): deleting or converting a node can leave a stale predecessor id on a
    dependent, and we don't want that to block autosave or commit. Per-node strip/self/dedupe
    already ran in ``WbsTaskInput._validate_node``; this adds the constraints that need whole-tree
    context (a node's kind, and which ids actually exist)."""
    is_leaf_by_id: dict[str, bool] = {}
    stack: list[WbsTaskInput] = list(tree)
    while stack:
        node = stack.pop()
        is_leaf_by_id[node.id] = node.is_leaf
        stack.extend(node.children)

    stack = list(tree)
    while stack:
        node = stack.pop()
        if node.depends_on:
            node.depends_on = [
                dep
                for dep in node.depends_on
                # same-kind + exists (`.get` is None for a dangling id → fails the equality)
                if is_leaf_by_id.get(dep) == node.is_leaf
            ]
        stack.extend(node.children)


class WbsDraftRequest(BaseModel):
    """POST /wbs/draft — generate (and persist) an LLM-drafted WBS tree."""

    model_config = ConfigDict(extra="forbid")
    project_name: str | None = None
    raw_input: str = Field(min_length=10, max_length=20000)
    stage2: Stage2Context | None = None
    stage3: Stage3Context | None = None
    # SDLC phases in scope for this engagement. A strict subset scopes the LLM-drafted tree to
    # those phases (the planner skips out-of-scope work); None / the full set ⇒ full lifecycle.
    selected_phases: list[Phase] | None = None


class WbsDraftResponse(BaseModel):
    """The drafted tree + the server-assigned ``draft_id`` it was persisted under."""

    model_config = ConfigDict(extra="forbid")
    draft_id: str
    tree: list[WbsTaskInput] = Field(default_factory=list)
    notes: str = Field(default="")
    # Token cost of the LLM planner call that drafted this tree (None when no API key / not captured).
    llm_usage: LlmUsage | None = None


class WbsDraft(BaseModel):
    """A resumable, server-persisted WBS draft (the full editor state)."""

    model_config = ConfigDict(extra="forbid")
    draft_id: str
    project_name: str = ""
    raw_input: str = ""
    tree: list[WbsTaskInput] = Field(default_factory=list)
    stage2: Stage2Context | None = None
    stage3: Stage3Context | None = None
    # WBS-only contingency reserve %, persisted with the draft so it survives resume. None on a
    # draft saved before this field existed → the editor seeds the default.
    contingency_pct: float | None = Field(default=None, ge=0, le=100)
    # Token cost of the LLM planner call that drafted this tree, persisted so the editor can show it
    # on resume. None on a pre-feature draft / when no API key was set.
    llm_usage: LlmUsage | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WbsDraftSummary(BaseModel):
    """One row in the "resume a draft" list."""

    model_config = ConfigDict(extra="forbid")
    draft_id: str
    project_name: str = ""
    task_count: int = 0
    updated_at: datetime | None = None


class WbsDraftList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[WbsDraftSummary] = Field(default_factory=list)
    # False when the draft store (Neo4j) is unavailable — the UI then notes that
    # resume needs Neo4j and falls back to its localStorage cache.
    resumable: bool = True


class WbsDraftSaveRequest(BaseModel):
    """PUT /wbs/drafts/{id} — autosave the editor state (draft_id comes from the path)."""

    model_config = ConfigDict(extra="forbid")
    project_name: str = ""
    raw_input: str = ""
    tree: list[WbsTaskInput] = Field(default_factory=list)
    stage2: Stage2Context | None = None
    stage3: Stage3Context | None = None
    contingency_pct: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _bound_tree(self) -> WbsDraftSaveRequest:
        _assert_tree_bounds(self.tree)
        _assert_unique_ids(self.tree)
        _sanitize_dependencies(self.tree)
        return self


class WbsCalculateRequest(BaseModel):
    """POST /estimates/wbs (+ /preview) — roll a finalized tree up into an estimate."""

    model_config = ConfigDict(extra="forbid")
    project_name: str | None = None
    raw_input: str = Field(default="", max_length=20000)
    draft_id: str | None = None  # the draft being committed (retired on success)
    tree: list[WbsTaskInput] = Field(min_length=1)
    stage2: Stage2Context | None = None
    stage3: Stage3Context | None = None
    # Explicit WBS contingency reserve %; None → the rollup applies WBS_DEFAULT_CONTINGENCY_PCT.
    contingency_pct: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _bound_tree(self) -> WbsCalculateRequest:
        _assert_tree_bounds(self.tree)
        _assert_unique_ids(self.tree)
        _sanitize_dependencies(self.tree)
        return self

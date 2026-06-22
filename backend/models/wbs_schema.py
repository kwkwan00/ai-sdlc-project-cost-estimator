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


class WbsDraftRequest(BaseModel):
    """POST /wbs/draft — generate (and persist) an LLM-drafted WBS tree."""

    model_config = ConfigDict(extra="forbid")
    project_name: str | None = None
    raw_input: str = Field(min_length=10, max_length=20000)
    stage2: Stage2Context | None = None
    stage3: Stage3Context | None = None


class WbsDraftResponse(BaseModel):
    """The drafted tree + the server-assigned ``draft_id`` it was persisted under."""

    model_config = ConfigDict(extra="forbid")
    draft_id: str
    tree: list[WbsTaskInput] = Field(default_factory=list)
    notes: str = Field(default="")


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
        return self

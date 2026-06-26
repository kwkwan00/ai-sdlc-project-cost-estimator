"""The WBS task-tree node + pure tree helpers.

A Work Breakdown Structure is a hierarchy: branches group work, leaves carry the
estimable effort. This node type is the single shape shared by the WBS draft, the
calculate request, the Neo4j graph rows, and the persisted estimate envelope.

It lives in its own leaf module (importing only ``Phase`` from ``twin_outputs``) so
``project_schema`` can reference it on ``EstimateEnvelope`` without a circular import
— ``models/wbs_schema.py`` (the request/draft models) imports BOTH this and
``project_schema``, and ``project_schema`` imports only this. One direction only.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.twin_outputs import Phase

# Bounds so a malformed / hostile tree can't blow up memory or the graph write. Enforced by
# `models.wbs_schema._assert_tree_bounds` (with MAX_WBS_DEPTH) on the user-submitted request
# models (`WbsCalculateRequest` / `WbsDraftSaveRequest`) → a too-large/too-deep tree 422s before
# reaching the recursive helpers (iter_leaves/count_tasks/flatten_tree/rebuild_tree) or the rollup.
MAX_WBS_NODES = 500


class WbsTaskInput(BaseModel):
    """One node in a WBS tree — a branch (has children) OR a leaf (carries the estimate).

    A node is a **leaf** iff it has no children. Leaves must carry a ``phase`` + a
    roster ``role_id`` + a three-point PERT estimate (the ordering is coerced, never
    raised, mirroring ``HourRange._coerce_pert_ordering``). Branches must NOT carry any
    estimate fields — those belong on the leaves they roll up.
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64, description="Stable id (editor key + Neo4j task_id)")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    # Leaf-only fields (None on branches).
    phase: Phase | None = None
    role_id: str | None = Field(default=None, max_length=64)
    optimistic: float | None = Field(default=None, ge=0)
    most_likely: float | None = Field(default=None, ge=0)
    pessimistic: float | None = Field(default=None, ge=0)
    children: list[WbsTaskInput] = Field(default_factory=list)
    # "Depends on" predecessor ids. Applies to BOTH kinds (unlike phase/role) — a work package may
    # depend on other work packages, a task on other tasks. The SAME-KIND + existence constraints
    # need sibling context, so they're enforced leniently at the tree level
    # (`wbs_schema._sanitize_dependencies`); here we only normalize locally (strip/self/dedup).
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_WBS_NODES)

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @model_validator(mode="after")
    def _validate_node(self) -> WbsTaskInput:
        # Normalize depends_on on BOTH kinds: strip ids, drop self-references and blanks, and dedupe
        # while preserving order. Cross-kind / dangling refs need sibling context and are pruned at
        # the tree level (`wbs_schema._sanitize_dependencies`).
        if self.depends_on:
            seen: set[str] = set()
            cleaned: list[str] = []
            for raw in self.depends_on:
                dep = raw.strip()
                if not dep or dep == self.id or dep in seen:
                    continue
                seen.add(dep)
                cleaned.append(dep)
            self.depends_on = cleaned
        if self.children:
            # Branch: reject leaf estimate fields rather than silently dropping them, so a
            # mislabeled node surfaces as a 422 instead of a wrong rollup.
            if any(
                v is not None
                for v in (self.phase, self.role_id, self.optimistic, self.most_likely, self.pessimistic)
            ):
                raise ValueError(
                    f"WBS branch {self.name!r} must not carry phase/role/hours "
                    "(those belong on its leaf descendants)"
                )
            return self
        # Leaf: phase + role_id are required for costing; the 3-point band is coerced.
        if self.phase is None:
            raise ValueError(f"WBS leaf {self.name!r} must have a phase")
        # Strip + reject a whitespace-only role_id at the boundary (a truthy "   " would otherwise
        # pass and round-trip/render as a meaningless badge before being folded at rollup).
        self.role_id = (self.role_id or "").strip()
        if not self.role_id:
            raise ValueError(f"WBS leaf {self.name!r} must have a role_id")
        o = self.optimistic or 0.0
        m = self.most_likely or 0.0
        p = self.pessimistic or 0.0
        lo, hi = min(o, m, p), max(o, m, p)
        self.optimistic, self.pessimistic = lo, hi
        self.most_likely = min(max(m, lo), hi)
        return self


WbsTaskInput.model_rebuild()


def iter_leaves(tree: list[WbsTaskInput]) -> Iterator[WbsTaskInput]:
    """Yield every leaf in document order (depth-first)."""
    for node in tree:
        if node.is_leaf:
            yield node
        else:
            yield from iter_leaves(node.children)


def count_tasks(tree: list[WbsTaskInput]) -> int:
    """Total node count (branches + leaves)."""
    return sum(1 + count_tasks(n.children) for n in tree)


def regenerate_ids(
    tree: list[WbsTaskInput], make_id: Callable[[], str]
) -> list[WbsTaskInput]:
    """Deep-copy the tree assigning a fresh id to every node (for Duplicate).

    ``make_id`` is injected (rather than calling ``uuid`` here) so the helper stays pure
    and unit-testable; the router passes ``lambda: str(uuid.uuid4())``.

    ``depends_on`` ids are remapped through the SAME old→new id map so a duplicated tree's
    dependency edges point at the copies, not the originals. A predecessor that somehow isn't in
    the map (a dangling ref) is dropped — the copy can't depend on a node it doesn't contain.
    """
    id_map: dict[str, str] = {}

    def relabel(nodes: list[WbsTaskInput]) -> list[WbsTaskInput]:
        out: list[WbsTaskInput] = []
        for node in nodes:
            new_id = make_id()
            id_map[node.id] = new_id
            out.append(node.model_copy(update={"id": new_id, "children": relabel(node.children)}))
        return out

    relabeled = relabel(tree)

    def remap(nodes: list[WbsTaskInput]) -> list[WbsTaskInput]:
        return [
            node.model_copy(
                update={
                    "depends_on": [id_map[d] for d in node.depends_on if d in id_map],
                    "children": remap(node.children),
                }
            )
            for node in nodes
        ]

    return remap(relabeled)


def flatten_tree(tree: list[WbsTaskInput], owner_id: str) -> list[dict]:
    """Flatten to graph rows for Neo4j. Each row carries its ``parent_id`` (the owner id
    for top-level nodes, else the parent task's id) and sibling ``order`` so
    ``rebuild_tree`` can reconstruct the exact nested shape."""
    rows: list[dict] = []

    def walk(nodes: list[WbsTaskInput], parent_id: str) -> None:
        for order, n in enumerate(nodes):
            rows.append(
                {
                    "task_id": n.id,
                    "parent_id": parent_id,
                    "owner_id": owner_id,
                    "order": order,
                    "name": n.name,
                    "description": n.description,
                    "phase": n.phase.value if n.phase else None,
                    "role_id": n.role_id,
                    "is_leaf": n.is_leaf,
                    "optimistic": n.optimistic,
                    "most_likely": n.most_likely,
                    "pessimistic": n.pessimistic,
                    "depends_on": list(n.depends_on),
                }
            )
            if n.children:
                walk(n.children, n.id)

    walk(tree, owner_id)
    return rows


def rebuild_tree(rows: list[dict], owner_id: str) -> list[WbsTaskInput]:
    """Inverse of ``flatten_tree``: reassemble the nested tree from flat graph rows.

    ``save_wbs_*`` rewrites the whole subgraph atomically (DETACH DELETE + UNWIND-rebuild), so the
    rows are normally a clean forest. The ``seen`` guard is defensive only: a cycle in a
    hand-edited / legacy graph would otherwise recurse forever — we visit each ``task_id`` at most
    once and drop the back-edge, degrading to a truncated tree instead of hanging."""
    by_parent: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_parent[r["parent_id"]].append(r)
    seen: set[str] = set()

    def build(parent_id: str) -> list[WbsTaskInput]:
        out: list[WbsTaskInput] = []
        for r in sorted(by_parent.get(parent_id, []), key=lambda x: x.get("order", 0)):
            task_id = r["task_id"]
            if task_id in seen:  # cycle / duplicate parent edge — skip the back-edge
                continue
            seen.add(task_id)
            children = build(task_id)
            leaf = not children
            out.append(
                WbsTaskInput(
                    id=r["task_id"],
                    name=r["name"],
                    description=r.get("description", "") or "",
                    phase=(r.get("phase") if leaf else None),
                    role_id=(r.get("role_id") if leaf else None),
                    optimistic=(r.get("optimistic") if leaf else None),
                    most_likely=(r.get("most_likely") if leaf else None),
                    pessimistic=(r.get("pessimistic") if leaf else None),
                    # depends_on applies to both kinds, so it's read regardless of leaf/branch.
                    depends_on=(r.get("depends_on") or []),
                    children=children,
                )
            )
        return out

    return build(owner_id)

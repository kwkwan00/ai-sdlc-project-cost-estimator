"""WBS "depends on" predecessor edges: per-node normalization, tree-level same-kind sanitization,
flatten/rebuild round-trip, and id-remap on duplicate."""

from __future__ import annotations

from models.twin_outputs import Phase
from models.wbs_schema import WbsCalculateRequest, _sanitize_dependencies
from models.wbs_task import (
    WbsTaskInput,
    flatten_tree,
    rebuild_tree,
    regenerate_ids,
)


def _leaf(node_id: str, name: str, depends_on: list[str] | None = None) -> WbsTaskInput:
    return WbsTaskInput(
        id=node_id,
        name=name,
        phase=Phase.DEVELOPMENT,
        role_id="sr_engineer",
        optimistic=1,
        most_likely=2,
        pessimistic=3,
        depends_on=depends_on or [],
    )


def _branch(
    node_id: str, name: str, children: list[WbsTaskInput], depends_on: list[str] | None = None
) -> WbsTaskInput:
    return WbsTaskInput(id=node_id, name=name, children=children, depends_on=depends_on or [])


# --- per-node normalization (runs inside WbsTaskInput._validate_node) -----------------------


def test_validator_strips_dedups_and_drops_self_reference() -> None:
    leaf = _leaf("l1", "Leaf", depends_on=["l2", "  l2 ", " l3 ", "l1", ""])
    # Whitespace stripped, "l2" de-duped, self ("l1") and blank dropped; order preserved.
    assert leaf.depends_on == ["l2", "l3"]


def test_depends_on_allowed_on_both_kinds() -> None:
    # A branch carrying depends_on is valid (unlike phase/role, which are leaf-only).
    branch = _branch("p2", "Pkg B", [_leaf("l1", "Leaf")], depends_on=["p1"])
    assert branch.depends_on == ["p1"]


# --- flatten / rebuild round-trip -----------------------------------------------------------


def test_flatten_rebuild_round_trips_depends_on_for_both_kinds() -> None:
    tree = [
        _branch("p1", "Pkg A", [_leaf("l1", "L1"), _leaf("l2", "L2", depends_on=["l1"])]),
        _branch("p2", "Pkg B", [_leaf("l3", "L3")], depends_on=["p1"]),
    ]
    rebuilt = rebuild_tree(flatten_tree(tree, "owner"), "owner")

    by_id = {n.id: n for n in rebuilt}
    assert by_id["p2"].depends_on == ["p1"]  # branch edge survives
    l2 = next(c for c in by_id["p1"].children if c.id == "l2")
    assert l2.depends_on == ["l1"]  # leaf edge survives
    # A row carries depends_on in the flat shape too.
    rows = {r["task_id"]: r for r in flatten_tree(tree, "owner")}
    assert rows["p2"]["depends_on"] == ["p1"]


# --- duplicate (regenerate_ids) remaps predecessor ids --------------------------------------


def test_regenerate_ids_remaps_depends_on_to_the_copies() -> None:
    tree = [
        _branch("p1", "A", [_leaf("l1", "L1"), _leaf("l2", "L2", depends_on=["l1"])]),
        _branch("p2", "B", [_leaf("l3", "L3")], depends_on=["p1"]),
    ]
    seq = iter(f"n{i}" for i in range(100))
    copied = regenerate_ids(tree, lambda: next(seq))

    by_name = {n.name: n for n in copied}
    a, b = by_name["A"], by_name["B"]
    # Every id is fresh, and B's dependency now points at the COPY of A (not the original "p1").
    assert a.id != "p1" and b.id != "p2"
    assert b.depends_on == [a.id]
    l1 = next(c for c in a.children if c.name == "L1")
    l2 = next(c for c in a.children if c.name == "L2")
    assert l2.depends_on == [l1.id] and l1.id != "l1"


def test_regenerate_ids_drops_dependency_on_node_outside_the_copied_subtree() -> None:
    # A predecessor id not present in the tree can't be remapped → dropped from the copy.
    tree = [_branch("p2", "B", [_leaf("l3", "L3")], depends_on=["ghost"])]
    seq = iter(f"n{i}" for i in range(100))
    copied = regenerate_ids(tree, lambda: next(seq))
    assert copied[0].depends_on == []


# --- tree-level sanitization (same-kind + existence) ----------------------------------------


def test_sanitize_keeps_valid_same_kind_edges() -> None:
    tree = [
        _branch("p1", "A", [_leaf("l1", "L1"), _leaf("l2", "L2", depends_on=["l1"])]),
        _branch("p2", "B", [_leaf("l3", "L3")], depends_on=["p1"]),
    ]
    _sanitize_dependencies(tree)
    assert tree[1].depends_on == ["p1"]
    assert tree[0].children[1].depends_on == ["l1"]


def test_sanitize_drops_cross_kind_and_dangling_edges() -> None:
    tree = [
        _branch("p1", "A", [_leaf("l1", "L1")]),
        _branch(
            "p2",
            "B",
            # branch depending on a leaf (cross-kind) + a leaf depending on a branch (cross-kind).
            [_leaf("l2", "L2", depends_on=["p1", "ghost"])],
            depends_on=["l1", "p1"],
        ),
    ]
    _sanitize_dependencies(tree)
    # p2 (branch): "l1" cross-kind dropped, "p1" same-kind kept.
    assert tree[1].depends_on == ["p1"]
    # l2 (leaf): "p1" cross-kind dropped, "ghost" dangling dropped → nothing left.
    assert tree[1].children[0].depends_on == []


# --- request model wires the sanitizer in ---------------------------------------------------


def test_calculate_request_sanitizes_dependencies() -> None:
    req = WbsCalculateRequest(
        tree=[
            _branch("p1", "A", [_leaf("l1", "L1")]),
            _branch("p2", "B", [_leaf("l2", "L2", depends_on=["p1"])], depends_on=["p1"]),
        ]
    )
    # leaf→branch ("p1") cross-kind pruned; branch→branch ("p1") kept.
    assert req.tree[1].depends_on == ["p1"]
    assert req.tree[1].children[0].depends_on == []


def test_calculate_request_rejects_duplicate_node_ids() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WbsCalculateRequest(
            tree=[
                _branch("p1", "A", [_leaf("dup", "L1")]),
                _branch("p2", "B", [_leaf("dup", "L2")]),  # duplicate leaf id "dup"
            ]
        )

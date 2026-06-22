"""WBS bottom-up rollup + tree helpers + the leaf Monte Carlo combine."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.project_schema import RoleRoster
from models.twin_outputs import Phase
from models.wbs_schema import WbsCalculateRequest
from models.wbs_task import (
    WbsTaskInput,
    count_tasks,
    flatten_tree,
    iter_leaves,
    rebuild_tree,
    regenerate_ids,
)
from orchestrator.montecarlo import combine_pert_leaves, make_rng
from orchestrator.wbs.rollup import _role_hours_for_phase, build_wbs_estimate


def _leaf(tid: str, phase: Phase, role: str, o: float, m: float, p: float) -> WbsTaskInput:
    return WbsTaskInput(
        id=tid, name=tid, phase=phase, role_id=role, optimistic=o, most_likely=m, pessimistic=p
    )


def _sample_tree() -> list[WbsTaskInput]:
    return [
        WbsTaskInput(
            id="pkg1",
            name="Build",
            children=[
                _leaf("l1", Phase.DEVELOPMENT, "sr_engineer", 10, 20, 40),
                _leaf("l2", Phase.DEVELOPMENT, "jr_engineer", 5, 10, 20),
            ],
        ),
        WbsTaskInput(
            id="pkg2",
            name="Test",
            children=[_leaf("l3", Phase.QA_TESTING, "sr_engineer", 4, 8, 16)],
        ),
    ]


# --- Monte Carlo leaf combine --------------------------------------------------------------


def test_combine_pert_leaves_point_and_identity() -> None:
    leaves = [(10.0, 20.0, 40.0), (5.0, 10.0, 20.0)]
    manual, ai = combine_pert_leaves(
        leaves, reduction_sampler=lambda rng: 0.25, eff_point=0.25, rng=make_rng("seed"), n_draws=500
    )
    # most_likely anchor = Σ modes; ai anchor = Σ modes·(1−eff)
    assert manual.point == pytest.approx(30.0)
    assert ai.point == pytest.approx(30.0 * 0.75)


def test_combine_pert_leaves_zero_reduction_makes_ai_equal_manual() -> None:
    leaves = [(10.0, 20.0, 40.0)]
    manual, ai = combine_pert_leaves(
        leaves, reduction_sampler=lambda rng: 0.0, eff_point=0.0, rng=make_rng("s"), n_draws=300
    )
    assert ai.point == pytest.approx(manual.point)
    assert ai.mean == pytest.approx(manual.mean)


def test_combine_pert_leaves_std_grows_with_spread() -> None:
    rng = make_rng("s")
    tight, _ = combine_pert_leaves(
        [(19.0, 20.0, 21.0)], reduction_sampler=lambda r: 0.0, eff_point=0.0, rng=rng, n_draws=800
    )
    wide, _ = combine_pert_leaves(
        [(2.0, 20.0, 80.0)], reduction_sampler=lambda r: 0.0, eff_point=0.0, rng=rng, n_draws=800
    )
    assert wide.std > tight.std


def test_combine_pert_leaves_empty_is_degenerate_zero() -> None:
    manual, ai = combine_pert_leaves(
        [], reduction_sampler=lambda r: 0.0, eff_point=0.0, rng=make_rng("s")
    )
    assert manual.point == 0.0 and ai.point == 0.0


def test_combine_pert_leaves_all_zero_bands_are_zero() -> None:
    manual, ai = combine_pert_leaves(
        [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)],
        reduction_sampler=lambda r: 0.3,
        eff_point=0.3,
        rng=make_rng("s"),
        n_draws=200,
    )
    assert manual.point == 0.0 and ai.point == 0.0
    assert manual.std == 0.0 and manual.mean == 0.0


def test_combine_pert_leaves_negative_reduction_makes_ai_slower() -> None:
    # AI net-slower (METR): a negative reduction must push AI hours ABOVE manual, and the
    # ai.most_likely == manual.most_likely·(1−eff) identity must still hold exactly.
    leaves = [(10.0, 20.0, 40.0)]
    manual, ai = combine_pert_leaves(
        leaves, reduction_sampler=lambda r: -0.15, eff_point=-0.15, rng=make_rng("s"), n_draws=400
    )
    assert ai.point == pytest.approx(manual.point * 1.15)
    assert ai.point > manual.point  # AI is slower
    assert ai.mean > manual.mean


# --- role-hours attribution ----------------------------------------------------------------


def test_role_hours_emits_exactly_one_row_per_roster_role() -> None:
    roster = RoleRoster.default()
    leaves = [
        _leaf("a", Phase.DEVELOPMENT, "sr_engineer", 1, 10, 20),
        _leaf("b", Phase.DEVELOPMENT, "jr_engineer", 1, 5, 8),
    ]
    out = _role_hours_for_phase(leaves, roster, scale=1.0)
    # One row per roster role, no extra synthetic OTHER rows, no duplicates.
    assert [r.role_id for r in out] == [r.role_id for r in roster.roles]
    assert sum(r.hours for r in out) == pytest.approx(15.0)


def test_role_hours_folds_unknown_role_into_a_real_roster_role() -> None:
    # A leaf with a stale/not-in-roster role_id must NOT spawn a synthetic OTHER row (synthesize
    # would drop it, understating cost + breaking Σ role_hours == most_likely). It must fold into
    # a real roster role so the hours stay costed even if the caller bypassed _remap_unknown_roles.
    # (Whitespace-only role_ids are rejected at the model boundary — see the validation test below.)
    roster = RoleRoster.default()
    valid = {r.role_id for r in roster.roles}
    leaves = [
        _leaf("b", Phase.DEVELOPMENT, "ghost_role", 1, 5, 8),  # not in roster
        _leaf("c", Phase.DEVELOPMENT, "sr_engineer", 1, 3, 6),  # real role
    ]
    out = _role_hours_for_phase(leaves, roster, scale=1.0)
    assert {r.role_id for r in out} <= valid  # never an out-of-roster / OTHER role_id
    assert len(out) == len(roster.roles)  # exactly one row per roster role
    # Both leaves' hours (5 + 3) preserved — nothing dropped.
    assert sum(r.hours for r in out) == pytest.approx(8.0)


def test_leaf_rejects_whitespace_only_role_id() -> None:
    # A whitespace-only role_id is truthy in Python, so it would slip past a bare `if not role_id`;
    # the validator strips + rejects it at the boundary rather than letting it round-trip.
    with pytest.raises(ValidationError):
        _leaf("w", Phase.DEVELOPMENT, "   ", 1, 2, 3)
    # A role_id with surrounding whitespace is normalized (stripped), not rejected.
    assert _leaf("x", Phase.DEVELOPMENT, "  sr_engineer  ", 1, 2, 3).role_id == "sr_engineer"


def test_role_hours_scale_applies_uniformly() -> None:
    roster = RoleRoster.default()
    leaves = [_leaf("a", Phase.DEVELOPMENT, "sr_engineer", 1, 10, 20)]
    out = _role_hours_for_phase(leaves, roster, scale=0.7)
    assert sum(r.hours for r in out) == pytest.approx(7.0)


async def test_build_wbs_estimate_uses_explicit_contingency_default_30(monkeypatch) -> None:
    # The WBS flow carries its OWN contingency: the request field, defaulting to 30% when omitted,
    # clamped to [0, 100]. It does NOT read the global app_settings the quick estimate uses.
    import db.postgres_adapter as pg

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)
    monkeypatch.setenv("MC_DRAWS", "120")

    # Omitted → 30% WBS default.
    final = await build_wbs_estimate(WbsCalculateRequest(tree=_sample_tree()), estimate_id="c1")
    assert final.contingency_pct == pytest.approx(30.0)
    # Explicit value is honored.
    final = await build_wbs_estimate(
        WbsCalculateRequest(tree=_sample_tree(), contingency_pct=35), estimate_id="c2"
    )
    assert final.contingency_pct == pytest.approx(35.0)
    # Zero is a real value (not "fall back to default").
    final = await build_wbs_estimate(
        WbsCalculateRequest(tree=_sample_tree(), contingency_pct=0), estimate_id="c3"
    )
    assert final.contingency_pct == pytest.approx(0.0)


# --- tree bounds (DoS guard on user-submitted requests) ------------------------------------


def test_calculate_request_rejects_too_many_nodes() -> None:
    from models.wbs_schema import MAX_WBS_NODES

    # One package + (MAX_WBS_NODES) leaves → over the cap → 422 before the rollup runs.
    leaves = [_leaf(f"l{i}", Phase.DEVELOPMENT, "sr_engineer", 1, 2, 3) for i in range(MAX_WBS_NODES)]
    tree = [WbsTaskInput(id="pkg", name="pkg", children=leaves)]
    with pytest.raises(ValidationError):
        WbsCalculateRequest(tree=tree)


def test_calculate_request_rejects_too_deep_a_tree() -> None:
    from models.wbs_schema import MAX_WBS_DEPTH

    # A chain of nested branches deeper than the cap → 422.
    node = _leaf("leaf", Phase.DEVELOPMENT, "sr_engineer", 1, 2, 3)
    for i in range(MAX_WBS_DEPTH + 1):
        node = WbsTaskInput(id=f"b{i}", name=f"b{i}", children=[node])
    with pytest.raises(ValidationError):
        WbsCalculateRequest(tree=[node])


def test_calculate_request_accepts_a_normal_tree() -> None:
    WbsCalculateRequest(tree=_sample_tree())  # must not raise


# --- node validation -----------------------------------------------------------------------


def test_branch_with_estimate_fields_raises() -> None:
    with pytest.raises(ValidationError):
        WbsTaskInput(
            id="b", name="b", phase=Phase.DEVELOPMENT, children=[_leaf("c", Phase.DEVELOPMENT, "r", 1, 2, 3)]
        )


def test_leaf_requires_phase_and_role() -> None:
    with pytest.raises(ValidationError):
        WbsTaskInput(id="l", name="l", role_id="r", optimistic=1, most_likely=2, pessimistic=3)
    with pytest.raises(ValidationError):
        WbsTaskInput(id="l", name="l", phase=Phase.DEVELOPMENT, optimistic=1, most_likely=2, pessimistic=3)


def test_leaf_coerces_pert_ordering() -> None:
    leaf = _leaf("l", Phase.DEVELOPMENT, "r", 40, 10, 5)  # out of order
    assert leaf.optimistic == 5 and leaf.pessimistic == 40
    assert (leaf.optimistic or 0) <= (leaf.most_likely or 0) <= (leaf.pessimistic or 0)


# --- tree helpers --------------------------------------------------------------------------


def test_flatten_rebuild_roundtrip() -> None:
    tree = _sample_tree()
    rows = flatten_tree(tree, "owner")
    rebuilt = rebuild_tree(rows, "owner")
    assert count_tasks(rebuilt) == count_tasks(tree)
    assert [leaf.id for leaf in iter_leaves(rebuilt)] == [leaf.id for leaf in iter_leaves(tree)]
    # leaf estimates survive the round-trip
    src = {leaf.id: leaf.most_likely for leaf in iter_leaves(tree)}
    out = {leaf.id: leaf.most_likely for leaf in iter_leaves(rebuilt)}
    assert src == out


def test_flatten_rebuild_roundtrip_deep_mixed_children() -> None:
    # 3 levels with a branch that has BOTH a leaf and a sub-branch child — ordering + nesting
    # must survive the flatten→rebuild round-trip.
    tree = [
        WbsTaskInput(
            id="top",
            name="top",
            children=[
                _leaf("lA", Phase.DEVELOPMENT, "sr_engineer", 1, 2, 3),
                WbsTaskInput(
                    id="sub",
                    name="sub",
                    children=[_leaf("lB", Phase.QA_TESTING, "sr_engineer", 4, 5, 6)],
                ),
                _leaf("lC", Phase.DEPLOYMENT, "sr_engineer", 7, 8, 9),
            ],
        )
    ]
    rebuilt = rebuild_tree(flatten_tree(tree, "owner"), "owner")
    assert count_tasks(rebuilt) == count_tasks(tree)
    # sibling ORDER preserved (leaf, branch, leaf) and the sub-branch nesting reconstructed.
    assert [c.id for c in rebuilt[0].children] == ["lA", "sub", "lC"]
    assert [leaf.id for leaf in iter_leaves(rebuilt)] == ["lA", "lB", "lC"]
    sub = next(c for c in rebuilt[0].children if c.id == "sub")
    assert not sub.is_leaf and [c.id for c in sub.children] == ["lB"]


def test_rebuild_tree_survives_a_cyclic_graph() -> None:
    # Atomic writes normally guarantee a clean forest, but a hand-edited / legacy graph could
    # contain a parent→descendant back-edge. rebuild_tree's `seen` guard must terminate (drop the
    # back-edge) rather than recurse forever. Rows: a→b, b→a (cycle) under owner→a.
    # After the back-edge is dropped: owner→a (branch) →b (leaf). 'b' becomes a leaf, so it must
    # carry valid leaf fields or WbsTaskInput construction (not the cycle guard) would fail.
    rows = [
        {"task_id": "a", "parent_id": "owner", "name": "A", "order": 0},
        {
            "task_id": "b", "parent_id": "a", "name": "B", "order": 0,
            "phase": "development", "role_id": "sr_engineer",
            "optimistic": 1, "most_likely": 2, "pessimistic": 3,
        },
        {"task_id": "a", "parent_id": "b", "name": "A-again", "order": 0},  # back-edge → dropped
    ]
    rebuilt = rebuild_tree(rows, "owner")  # must return, not hang
    all_ids = {c.id for top in rebuilt for c in [top, *top.children]}
    assert all_ids == {"a", "b"}  # both nodes appear once; the cycle is broken


def test_regenerate_ids_are_disjoint_from_source() -> None:
    tree = _sample_tree()
    counter = [0]

    def _mk() -> str:
        counter[0] += 1
        return f"new-{counter[0]}"

    dup = regenerate_ids(tree, _mk)
    src_ids = {n.id for n in tree} | {leaf.id for leaf in iter_leaves(tree)}
    dup_ids = {n.id for n in dup} | {leaf.id for leaf in iter_leaves(dup)}
    assert src_ids.isdisjoint(dup_ids)
    # structure preserved
    assert count_tasks(dup) == count_tasks(tree)


# --- end-to-end rollup ---------------------------------------------------------------------


async def test_build_wbs_estimate_invariants(monkeypatch) -> None:
    import db.postgres_adapter as pg

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)
    monkeypatch.setenv("MC_DRAWS", "200")

    req = WbsCalculateRequest(tree=_sample_tree())
    final = await build_wbs_estimate(req, estimate_id="e1")

    # Two phases present (development + qa_testing).
    assert {p.phase for p in final.phases} == {Phase.DEVELOPMENT, Phase.QA_TESTING}
    # most_likely is Σ leaf modes (30 dev + 8 qa = 38) on the manual scenario.
    assert final.total_manual_only_hours.most_likely == pytest.approx(38.0, rel=1e-6)
    for p in final.phases:
        eff = p.effective_ai_reduction_pct / 100.0
        assert p.ai_assisted_hours.most_likely == pytest.approx(
            p.manual_only_hours.most_likely * (1 - eff)
        )
        # role hours sum to most_likely (manual) and to ai-most_likely (ai)
        assert sum(r.hours for r in p.manual_only_role_hours) == pytest.approx(
            p.manual_only_hours.most_likely
        )
        assert sum(r.hours for r in p.ai_assisted_role_hours) == pytest.approx(
            p.ai_assisted_hours.most_likely
        )
    # MC band populated (percentiles present for the fan chart).
    assert final.total_manual_only_hours.percentiles is not None


async def test_build_wbs_estimate_empty_phase_grouping(monkeypatch) -> None:
    import db.postgres_adapter as pg

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)
    # Single dev leaf → exactly one phase estimate.
    req = WbsCalculateRequest(
        tree=[_leaf("only", Phase.DEVELOPMENT, "sr_engineer", 8, 16, 32)]
    )
    final = await build_wbs_estimate(req, estimate_id="e2")
    assert [p.phase for p in final.phases] == [Phase.DEVELOPMENT]
    assert final.headcount_by_role  # synthesize produced a staffing table


async def test_build_wbs_estimate_remaps_unknown_role_into_costed_role(monkeypatch) -> None:
    # A leaf referencing a role_id NOT in the roster (stale/hand-edited tree) must not have its
    # hours silently dropped from the headcount table + cost (synthesize iterates roster roles
    # only). The rollup remaps it onto a real roster role so the hours stay staffed + costed.
    import db.postgres_adapter as pg

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)

    req = WbsCalculateRequest(
        tree=[_leaf("only", Phase.DEVELOPMENT, "ghost_role", 8, 16, 32)]
    )
    final = await build_wbs_estimate(req, estimate_id="ghost")

    # The phantom role_id is gone — hours absorbed by a real roster role.
    phase_role_ids = {rh.role_id for rh in final.phases[0].manual_only_role_hours}
    assert "ghost_role" not in phase_role_ids
    assert phase_role_ids <= {r.role_id for r in RoleRoster.default().roles}
    # Hours are staffed (headcount > 0 somewhere) and costed (> $0), not dropped.
    assert any(h.headcount > 0 for h in final.headcount_by_role)
    assert any(h.manual_only_hours == pytest.approx(16.0) for h in final.headcount_by_role)
    assert final.total_cost_manual_only_usd > 0.0
    # The caller's request leaf is NOT mutated (remap copies).
    assert req.tree[0].role_id == "ghost_role"


def test_default_roster_role_ids_present() -> None:
    # The sample tree references roster role_ids that exist in the default roster.
    roster_ids = {r.role_id for r in RoleRoster.default().roles}
    assert {"sr_engineer", "jr_engineer"} <= roster_ids


async def test_build_wbs_estimate_is_deterministic_for_fixed_id(monkeypatch) -> None:
    # Same tree + same estimate_id ⇒ byte-identical bands and cost (MC is seeded on the id). The
    # router passes different ids for preview vs commit; given a FIXED id the result reproduces.
    import db.postgres_adapter as pg

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)
    monkeypatch.setenv("MC_DRAWS", "200")

    req = WbsCalculateRequest(tree=_sample_tree())
    a = await build_wbs_estimate(req, estimate_id="fixed")
    b = await build_wbs_estimate(req, estimate_id="fixed")
    assert a.total_manual_only_hours.model_dump() == b.total_manual_only_hours.model_dump()
    assert a.total_ai_assisted_hours.model_dump() == b.total_ai_assisted_hours.model_dump()
    assert a.total_cost_manual_only_usd == b.total_cost_manual_only_usd


async def test_build_wbs_estimate_zero_hour_phase_keeps_role_sum_identity(monkeypatch) -> None:
    # A phase whose leaves are all-zero hours must still satisfy Σ role_hours == most_likely == 0
    # on BOTH scenarios, even though the phase carries a non-zero AI reduction.
    import db.postgres_adapter as pg

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)
    monkeypatch.setenv("MC_DRAWS", "200")

    req = WbsCalculateRequest(tree=[_leaf("z", Phase.DEVELOPMENT, "sr_engineer", 0, 0, 0)])
    final = await build_wbs_estimate(req, estimate_id="zero")
    p = final.phases[0]
    assert p.manual_only_hours.most_likely == 0.0
    assert p.ai_assisted_hours.most_likely == 0.0
    assert sum(r.hours for r in p.manual_only_role_hours) == pytest.approx(0.0)
    assert sum(r.hours for r in p.ai_assisted_role_hours) == pytest.approx(0.0)


async def test_build_wbs_estimate_brownfield_regulated_keeps_role_sum_identity(monkeypatch) -> None:
    # Penalized codebase (familiar-large brownfield) + regulated flag shrink the realized reduction
    # toward / below zero. The load-bearing identity Σ ai_role_hours == ai.most_likely must hold
    # EXACTLY (both use the TRUE eff, not the display-rounded effective_ai_reduction_pct), and
    # ai.most_likely must track manual.most_likely·(1−eff) within the pct field's 0.1-rounding.
    import db.postgres_adapter as pg
    from models.project_schema import (
        AiToolingLevel,
        CodebaseContext,
        PhaseToolingLevels,
        Stage2Context,
        Stage3Context,
    )

    pg._reset_for_tests()
    monkeypatch.setattr(pg, "get_sessionmaker", lambda: None)
    monkeypatch.setenv("MC_DRAWS", "200")

    stage2 = Stage2Context(regulatory_requirements=["HIPAA", "SOC2"])
    stage3 = Stage3Context(
        codebase_context=CodebaseContext.BROWNFIELD_LARGE_FAMILIAR,
        ai_tooling=PhaseToolingLevels(development=AiToolingLevel.AGENTIC),
    )
    req = WbsCalculateRequest(
        tree=[_leaf("d", Phase.DEVELOPMENT, "sr_engineer", 10, 20, 40)],
        stage2=stage2,
        stage3=stage3,
    )
    final = await build_wbs_estimate(req, estimate_id="neg")
    p = final.phases[0]
    # Exact identity (true eff): role hours scaled by (1−eff) sum to ai.most_likely.
    assert sum(r.hours for r in p.ai_assisted_role_hours) == pytest.approx(
        p.ai_assisted_hours.most_likely
    )
    assert sum(r.hours for r in p.manual_only_role_hours) == pytest.approx(
        p.manual_only_hours.most_likely
    )
    # Display-rounded pct reconstructs ai.most_likely to within the 0.1% rounding band.
    eff_rounded = p.effective_ai_reduction_pct / 100.0
    assert p.ai_assisted_hours.most_likely == pytest.approx(
        p.manual_only_hours.most_likely * (1 - eff_rounded), abs=0.05
    )

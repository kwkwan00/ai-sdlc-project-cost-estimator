"""WBS planner agent: deterministic fallback + the lenient-output backstop."""

from __future__ import annotations

from agents import wbs_agent
from agents.wbs_agent import (
    WbsPlannerLeaf,
    WbsPlannerPackage,
    WbsPlannerResponse,
    _build_user_prompt,
    _complexity_effort_factor,
    _effective_stage2_for_factor,
    _fallback_tree,
    _planner_to_tree,
    generate_wbs_tree,
    run_wbs_planner,
)
from models.project_schema import (
    CodebaseContext,
    ProjectType,
    RoleRoster,
    Stage2Context,
    Stage3Context,
)
from models.twin_outputs import Phase
from models.wbs_schema import WbsDraftRequest
from models.wbs_task import iter_leaves
from orchestrator.nodes.parse_input import ParsedContext


def test_fallback_tree_is_leaf_complete() -> None:
    tree = _fallback_tree(RoleRoster.default())
    leaves = list(iter_leaves(tree))
    assert leaves, "fallback must produce at least one leaf"
    # Every leaf is valid for costing: phase + roster role + a 3-point band.
    roster_ids = {r.role_id for r in RoleRoster.default().roles}
    for leaf in leaves:
        assert leaf.phase is not None
        assert leaf.role_id in roster_ids
        assert (leaf.optimistic or 0) <= (leaf.most_likely or 0) <= (leaf.pessimistic or 0)
    # Covers the whole lifecycle.
    assert {leaf.phase for leaf in leaves} == set(Phase)


def test_planner_backstop_fills_role_and_phase_and_order() -> None:
    resp = WbsPlannerResponse(
        packages=[
            WbsPlannerPackage(
                name="Build",
                tasks=[
                    # invalid role_id + unknown phase + out-of-order hours
                    WbsPlannerLeaf(
                        name="thing", phase="nonsense", role_id="ghost",
                        optimistic=40, most_likely=10, pessimistic=5,
                    )
                ],
            )
        ]
    )
    tree = _planner_to_tree(resp, RoleRoster.default())
    leaves = list(iter_leaves(tree))
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.role_id in {r.role_id for r in RoleRoster.default().roles}  # snapped to a real role
    assert leaf.phase == Phase.DEVELOPMENT  # unknown phase → default
    assert leaf.optimistic == 5 and leaf.pessimistic == 40  # ordering coerced


def test_planner_drops_empty_packages() -> None:
    resp = WbsPlannerResponse(packages=[WbsPlannerPackage(name="Empty", tasks=[])])
    assert _planner_to_tree(resp, RoleRoster.default()) == []


def _planner_leaf(name: str, key: str, depends_on: list[str] | None = None) -> WbsPlannerLeaf:
    return WbsPlannerLeaf(
        name=name, key=key, phase="development", role_id="sr_engineer",
        optimistic=1, most_likely=2, pessimistic=3, depends_on=depends_on or [],
    )


def test_planner_translates_dependency_keys_to_real_ids() -> None:
    resp = WbsPlannerResponse(
        packages=[
            WbsPlannerPackage(name="A", key="pkg-a", tasks=[_planner_leaf("design", "t-design")]),
            WbsPlannerPackage(
                name="B", key="pkg-b", depends_on=["pkg-a"],
                tasks=[_planner_leaf("impl", "t-impl", depends_on=["t-design"])],
            ),
        ]
    )
    tree = _planner_to_tree(resp, RoleRoster.default())
    by_name = {n.name: n for n in tree}
    a, b = by_name["A"], by_name["B"]
    # Package B's dependency now points at A's REAL id (not the model's "pkg-a" key).
    assert b.depends_on == [a.id]
    assert "pkg-a" not in b.depends_on
    # Same for the leaf edge.
    design, impl = a.children[0], b.children[0]
    assert impl.depends_on == [design.id]
    assert "t-design" not in impl.depends_on


def test_planner_drops_cross_kind_and_unknown_dependencies() -> None:
    resp = WbsPlannerResponse(
        packages=[
            WbsPlannerPackage(
                name="A", key="pkg-a",
                # package → leaf key (cross-kind) + unknown key: both dropped.
                depends_on=["t-x", "ghost"],
                tasks=[
                    _planner_leaf("x", "t-x"),
                    # leaf → package key (cross-kind) + unknown key: both dropped.
                    _planner_leaf("y", "t-y", depends_on=["pkg-a", "nope"]),
                ],
            )
        ]
    )
    pkg = _planner_to_tree(resp, RoleRoster.default())[0]
    assert pkg.depends_on == []
    y = next(c for c in pkg.children if c.name == "y")
    assert y.depends_on == []


def test_planner_drops_self_dependency() -> None:
    resp = WbsPlannerResponse(
        packages=[WbsPlannerPackage(name="A", key="pkg-a", tasks=[_planner_leaf("x", "t-x", depends_on=["t-x"])])]
    )
    leaf = list(iter_leaves(_planner_to_tree(resp, RoleRoster.default())))[0]
    assert leaf.depends_on == []


def test_fallback_tree_chains_dependencies_through_the_lifecycle() -> None:
    leaves = list(iter_leaves(_fallback_tree(RoleRoster.default())))
    assert len(leaves) >= 2
    assert leaves[0].depends_on == []  # the first task has no predecessor
    for prev, leaf in zip(leaves, leaves[1:], strict=False):
        assert leaf.depends_on == [prev.id]  # linear lifecycle chain


def test_complexity_effort_factor_grows_with_project_complexity() -> None:
    # No context → just the base factor.
    assert _complexity_effort_factor(None, None) == wbs_agent._EFFORT_BASE

    simple = _complexity_effort_factor(
        Stage2Context(project_type=ProjectType.GREENFIELD, integration_count=1, screen_count_estimate=6),
        Stage3Context(codebase_context=CodebaseContext.GREENFIELD),
    )
    complex_regulated = _complexity_effort_factor(
        Stage2Context(
            project_type=ProjectType.LEGACY_REPLACEMENT,
            integration_count=8,
            screen_count_estimate=60,
            regulatory_requirements=["HIPAA", "SOC 2"],
        ),
        Stage3Context(codebase_context=CodebaseContext.BROWNFIELD_LARGE_UNFAMILIAR),
    )
    # A simple greenfield project gets a light correction; a regulated, multi-integration,
    # unfamiliar-brownfield project gets a much larger one — and both stay within the band.
    assert simple < complex_regulated
    assert wbs_agent._EFFORT_MIN <= simple <= wbs_agent._EFFORT_MAX
    assert wbs_agent._EFFORT_MIN <= complex_regulated <= wbs_agent._EFFORT_MAX
    assert complex_regulated >= 2.5  # heavy hidden work → strong uplift


def test_complexity_effort_factor_is_clamped_and_scale_guarded() -> None:
    s2 = Stage2Context(regulatory_requirements=["HIPAA", "SOC 2", "PCI-DSS"], integration_count=20,
                       screen_count_estimate=200)
    s3 = Stage3Context(codebase_context=CodebaseContext.BROWNFIELD_LARGE_UNFAMILIAR)
    # A huge scale can't push it past the cap; a non-positive scale degrades to 1.0 (never zeroes).
    assert _complexity_effort_factor(s2, s3, scale=100.0) == wbs_agent._EFFORT_MAX
    assert _complexity_effort_factor(s2, s3, scale=0.0) >= wbs_agent._EFFORT_MIN


def test_effort_multiplier_scales_drafted_hours() -> None:
    # The bottom-up realism factor scales (and rounds) the LLM-drafted leaf hours; default 1.0 leaves
    # them as-is. Corrects the systematic optimism of bottom-up task estimates.
    resp = WbsPlannerResponse(
        packages=[
            WbsPlannerPackage(
                name="Build",
                tasks=[
                    WbsPlannerLeaf(
                        name="t", phase="development", role_id="sr_engineer",
                        optimistic=10, most_likely=20, pessimistic=40,
                    )
                ],
            )
        ]
    )
    base = list(iter_leaves(_planner_to_tree(resp, RoleRoster.default())))[0]
    assert (base.optimistic, base.most_likely, base.pessimistic) == (10, 20, 40)  # 1.0 = no change
    scaled = list(
        iter_leaves(_planner_to_tree(resp, RoleRoster.default(), effort_multiplier=2.0))
    )[0]
    assert (scaled.optimistic, scaled.most_likely, scaled.pessimistic) == (20, 40, 80)
    # A non-positive multiplier degrades to 1.0 rather than zeroing every estimate.
    safe = list(
        iter_leaves(_planner_to_tree(resp, RoleRoster.default(), effort_multiplier=0.0))
    )[0]
    assert safe.most_likely == 20


def test_effective_stage2_fills_complexity_signals_from_parsed() -> None:
    # The WBS wizard collects only roster + codebase, so the complexity signals the realism factor
    # reads (project type, integrations, screens, regulatory) must come from the parsed description
    # or the factor is inert on the real flow. A neutral stage2 inherits all of them from `parsed`.
    parsed = ParsedContext(
        project_type_hint="integration",
        screen_count_estimate=40,
        integration_mentions=["Salesforce", "Stripe"],
        regulatory_mentions=["HIPAA", "HIPAA", "SOC 2"],  # dupes collapse
    )
    eff = _effective_stage2_for_factor(Stage2Context(), parsed)
    assert eff.project_type is ProjectType.INTEGRATION
    assert eff.integration_count == 2
    assert eff.screen_count_estimate == 40
    assert eff.regulatory_requirements == ["HIPAA", "SOC 2"]
    # And it actually moves the factor above the base (otherwise the fix is cosmetic).
    assert _complexity_effort_factor(eff, None) > wbs_agent._EFFORT_BASE


def test_effective_stage2_prefers_explicit_non_default_values() -> None:
    # A future richer wizard that DID collect a non-default value must win over the inferred one.
    parsed = ParsedContext(project_type_hint="greenfield", integration_mentions=["X"])
    explicit = Stage2Context(project_type=ProjectType.LEGACY_REPLACEMENT, integration_count=7)
    eff = _effective_stage2_for_factor(explicit, parsed)
    assert eff.project_type is ProjectType.LEGACY_REPLACEMENT
    assert eff.integration_count == 7


async def test_run_wbs_planner_requests_large_max_tokens(monkeypatch) -> None:
    """A full WBS (dozens of leaf tasks) overflows the 4096 default, truncating the `packages`
    array mid-output so the tool input arrives empty and the agent falls back to the generic
    skeleton. Guard that the planner asks for a generous token budget."""
    captured: dict = {}

    async def _fake_call_structured(**kwargs):
        captured.update(kwargs)
        return WbsPlannerResponse(
            packages=[WbsPlannerPackage(name="P", tasks=[WbsPlannerLeaf(name="t")])]
        )

    monkeypatch.setattr(wbs_agent, "call_structured", _fake_call_structured)
    resp = await run_wbs_planner(WbsDraftRequest(raw_input="Build a small expense tracker app."))
    assert resp.packages
    assert captured.get("max_tokens", 4096) >= 8192


async def test_generate_wbs_tree_degrades_without_api_key() -> None:
    # No ANTHROPIC_API_KEY in the test env → the agent must still return an editable tree.
    tree, _notes = await generate_wbs_tree(
        WbsDraftRequest(raw_input="Build a small internal tool for tracking expenses.")
    )
    assert list(iter_leaves(tree)), "must fall back to a non-empty skeleton"


# ---------- phase scoping (don't draft work for disabled phases) ----------


def _planner_leaf_for(name: str, key: str, phase: str, depends_on: list[str] | None = None) -> WbsPlannerLeaf:
    return WbsPlannerLeaf(
        name=name, key=key, phase=phase, role_id="sr_engineer",
        optimistic=1, most_likely=2, pessimistic=3, depends_on=depends_on or [],
    )


def test_planner_drops_packages_for_out_of_scope_phases() -> None:
    resp = WbsPlannerResponse(
        packages=[
            WbsPlannerPackage(name="Dev", key="p-dev", tasks=[_planner_leaf_for("build", "t-b", "development")]),
            WbsPlannerPackage(name="Design", key="p-ux", tasks=[_planner_leaf_for("wireframes", "t-w", "ux_design")]),
        ]
    )
    tree = _planner_to_tree(resp, RoleRoster.default(), selected_phases=[Phase.DEVELOPMENT])
    # The ux_design package is dropped entirely — its only leaf was out of scope.
    assert {n.name for n in tree} == {"Dev"}
    assert {leaf.phase for leaf in iter_leaves(tree)} == {Phase.DEVELOPMENT}


def test_planner_scope_prunes_dependencies_onto_dropped_tasks() -> None:
    # A kept dev task depends on a dropped ux task → that edge is pruned (its key never resolves).
    resp = WbsPlannerResponse(
        packages=[
            WbsPlannerPackage(name="A", key="p-a", tasks=[
                _planner_leaf_for("wf", "t-wf", "ux_design"),
                _planner_leaf_for("build", "t-build", "development", depends_on=["t-wf"]),
            ]),
        ]
    )
    leaves = list(iter_leaves(_planner_to_tree(resp, RoleRoster.default(), selected_phases=[Phase.DEVELOPMENT])))
    assert [leaf.name for leaf in leaves] == ["build"]
    assert leaves[0].depends_on == []


def test_fallback_tree_scoped_to_selected_phases() -> None:
    phases = {leaf.phase for leaf in iter_leaves(_fallback_tree(RoleRoster.default(), [Phase.DEVELOPMENT, Phase.QA_TESTING]))}
    assert phases == {Phase.DEVELOPMENT, Phase.QA_TESTING}  # only in-scope phases appear


def test_build_user_prompt_emits_scope_block_for_a_subset() -> None:
    from models.project_schema import Stage3Context

    req = WbsDraftRequest(
        raw_input="Build something substantial here.",
        selected_phases=[Phase.DEVELOPMENT, Phase.QA_TESTING],
    )
    prompt = _build_user_prompt(req, RoleRoster.default(), Stage3Context())
    assert "ONLY these SDLC phases — development, qa_testing" in prompt
    assert "Do NOT create any work package or leaf task for a phase outside this set" in prompt


def test_build_user_prompt_has_no_scope_block_for_full_lifecycle() -> None:
    from models.project_schema import Stage3Context

    req = WbsDraftRequest(raw_input="Build something substantial here.")  # selected_phases=None
    prompt = _build_user_prompt(req, RoleRoster.default(), Stage3Context())
    assert "SCOPE:" not in prompt


async def test_generate_wbs_tree_scopes_fallback_without_api_key() -> None:
    # No API key → degrades to the deterministic skeleton, which must still honor the phase scope.
    tree, _ = await generate_wbs_tree(
        WbsDraftRequest(
            raw_input="Build a small internal tool for tracking expenses.",
            selected_phases=[Phase.DEVELOPMENT],
        )
    )
    assert {leaf.phase for leaf in iter_leaves(tree)} == {Phase.DEVELOPMENT}


def test_planner_resolves_colliding_leaf_keys_within_the_same_package() -> None:
    # LLM reuses the slug "tests" in two packages; Billing's integration depends_on=["tests"] must
    # resolve to BILLING's tests (same package), not Auth's (global first-wins would misroute it).
    resp = WbsPlannerResponse(
        packages=[
            WbsPlannerPackage(name="Auth", key="p-auth", tasks=[_planner_leaf("auth tests", "tests")]),
            WbsPlannerPackage(
                name="Billing", key="p-bill",
                tasks=[
                    _planner_leaf("billing tests", "tests"),
                    _planner_leaf("billing integration", "integ", depends_on=["tests"]),
                ],
            ),
        ]
    )
    tree = _planner_to_tree(resp, RoleRoster.default())
    billing = next(n for n in tree if n.name == "Billing")
    billing_tests = next(c for c in billing.children if c.name == "billing tests")
    integ = next(c for c in billing.children if c.name == "billing integration")
    assert integ.depends_on == [billing_tests.id]  # same-package, not Auth's "tests"

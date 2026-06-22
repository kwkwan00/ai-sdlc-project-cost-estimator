"""WBS planner agent: deterministic fallback + the lenient-output backstop."""

from __future__ import annotations

import wbs_agent
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
from wbs_agent import (
    WbsPlannerLeaf,
    WbsPlannerPackage,
    WbsPlannerResponse,
    _complexity_effort_factor,
    _effective_stage2_for_factor,
    _fallback_tree,
    _planner_to_tree,
    generate_wbs_tree,
    run_wbs_planner,
)


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

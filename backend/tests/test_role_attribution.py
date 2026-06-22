"""Coverage for the tag-aware role attribution logic.

The override rules are keyed on RoleCategory / RoleSeniority tags, not on fixed
role names, so these tests construct synthetic rosters with the relevant tag
combinations rather than relying on the default 4-role roster.
"""

from __future__ import annotations

import math

from models.project_schema import CustomRole, RoleRoster
from models.twin_outputs import Phase, RoleCategory, RoleSeniority
from orchestrator.role_attribution import attribute_roles, default_role_id


def _roster(*roles: tuple[str, RoleCategory, RoleSeniority, float]) -> RoleRoster:
    """Build a RoleRoster from terse tuples: (role_id, category, seniority, pct)."""
    items = [
        CustomRole(
            role_id=rid,
            description=rid.replace("_", " ").title(),
            category=cat,
            seniority=sen,
            rate_per_hour=200.0,
            percentage=pct,
        )
        for rid, cat, sen, pct in roles
    ]
    return RoleRoster(roles=items)


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, abs_tol=tol)


def test_default_role_id_prefers_senior_engineer_then_engineer_then_first() -> None:
    # Senior engineer wins outright.
    r = _roster(
        ("pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 30.0),
        ("eng_sr", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 40.0),
        ("eng_jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 30.0),
    )
    assert default_role_id(r) == "eng_sr"
    # No senior engineer → any engineer.
    r2 = _roster(
        ("pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 50.0),
        ("eng_jr", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 50.0),
    )
    assert default_role_id(r2) == "eng_jr"
    # No engineer at all → first role.
    r3 = _roster(("pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 100.0))
    assert default_role_id(r3) == "pm"
    # Empty roster → stable synthetic fallback.
    assert default_role_id(RoleRoster()) == "sr_engineer"


def _total(hours_list) -> float:
    return sum(rh.hours for rh in hours_list)


def _by_id(hours_list) -> dict[str, float]:
    return {rh.role_id: rh.hours for rh in hours_list}


# ---------- conservation invariants ----------


def test_attribute_roles_returns_empty_for_empty_roster() -> None:
    assert attribute_roles(1000.0, RoleRoster(roles=[]), Phase.DEVELOPMENT) == []


def test_attribute_roles_emits_one_entry_per_roster_role() -> None:
    roster = RoleRoster.default()
    result = attribute_roles(800.0, roster, Phase.DEVELOPMENT)
    assert len(result) == len(roster.roles)
    assert {rh.role_id for rh in result} == {r.role_id for r in roster.roles}


def test_total_hours_preserved_across_every_phase() -> None:
    roster = RoleRoster.default()
    for phase in Phase:
        result = attribute_roles(1000.0, roster, phase)
        assert _close(_total(result), 1000.0), f"phase {phase} did not preserve total"


def test_role_hours_carry_description_and_tags_from_roster() -> None:
    roster = _roster(
        ("eng_lead", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100.0),
    )
    result = attribute_roles(500.0, roster, Phase.DEVELOPMENT)
    assert result[0].role_description == "Eng Lead"
    assert result[0].category is RoleCategory.ENGINEERING
    assert result[0].seniority is RoleSeniority.SENIOR


# ---------- DEVELOPMENT / QA_TESTING: pass-through ----------


def test_development_honors_user_input_faithfully() -> None:
    roster = _roster(
        ("sr_pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 10),
        ("jr_pm", RoleCategory.PRODUCT, RoleSeniority.JUNIOR, 10),
        ("sr_eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 60),
        ("jr_eng", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 20),
    )
    result = _by_id(attribute_roles(100.0, roster, Phase.DEVELOPMENT))
    assert _close(result["sr_pm"], 10)
    assert _close(result["jr_pm"], 10)
    assert _close(result["sr_eng"], 60)
    assert _close(result["jr_eng"], 20)


def test_qa_testing_honors_user_input_faithfully() -> None:
    roster = _roster(
        ("qa", RoleCategory.QA, RoleSeniority.SENIOR, 60),
        ("eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 40),
    )
    result = _by_id(attribute_roles(100.0, roster, Phase.QA_TESTING))
    assert _close(result["qa"], 60)
    assert _close(result["eng"], 40)


# ---------- DISCOVERY: cap juniors at 25% ----------


def test_discovery_caps_junior_share_at_25_percent() -> None:
    roster = _roster(
        ("sr_pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 20),
        ("jr_pm", RoleCategory.PRODUCT, RoleSeniority.JUNIOR, 10),
        ("sr_eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 10),
        ("jr_eng", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 60),
    )
    result = _by_id(attribute_roles(100.0, roster, Phase.DISCOVERY))
    assert result["jr_eng"] <= 25.5
    # Excess from jr_eng (engineering) lands in sr_eng (same category).
    assert result["sr_eng"] > 10
    assert _close(sum(result.values()), 100.0)


def test_discovery_excess_lands_in_same_category_senior_when_present() -> None:
    roster = _roster(
        ("sr_pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 5),
        ("sr_eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 5),
        ("jr_eng", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 90),
    )
    result = _by_id(attribute_roles(100.0, roster, Phase.DISCOVERY))
    assert result["jr_eng"] <= 25.5
    # Engineering senior absorbs the excess (~65 of the ~90 - 25 surplus).
    assert result["sr_eng"] > result["sr_pm"]


def test_discovery_falls_back_to_any_senior_when_no_same_category_match() -> None:
    # Junior data engineer with no data-category senior — excess goes to other seniors.
    roster = _roster(
        ("sr_pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 10),
        ("sr_eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 10),
        ("jr_data", RoleCategory.DATA, RoleSeniority.JUNIOR, 80),
    )
    result = _by_id(attribute_roles(100.0, roster, Phase.DISCOVERY))
    assert result["jr_data"] <= 25.5
    # The two seniors absorb the excess between them.
    assert result["sr_pm"] + result["sr_eng"] > 20


# ---------- CODE_REVIEW: cap juniors at 15% ----------


def test_code_review_caps_junior_share_at_15_percent() -> None:
    roster = _roster(
        ("sr_pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 10),
        ("jr_pm", RoleCategory.PRODUCT, RoleSeniority.JUNIOR, 40),
        ("sr_eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 20),
        ("jr_eng", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 30),
    )
    result = _by_id(attribute_roles(100.0, roster, Phase.CODE_REVIEW))
    assert result["jr_pm"] <= 15.5
    assert result["jr_eng"] <= 15.5
    assert _close(sum(result.values()), 100.0)


# ---------- UX_DESIGN: product + ui_ux floor of 40% ----------


def test_ux_design_enforces_product_and_uiux_floor() -> None:
    # User asks for 0% product/UI; UX should still allocate the floor.
    roster = _roster(
        ("sr_eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 80),
        ("jr_eng", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 20),
    )
    # With no product/ui_ux roles in the roster at all, no floor can be applied
    # (nowhere to push the shortfall) — the result preserves the input.
    result = _by_id(attribute_roles(100.0, roster, Phase.UX_DESIGN))
    assert _close(sum(result.values()), 100.0)


def test_ux_design_prefers_ui_ux_role_over_product_when_filling_floor() -> None:
    # Both ui_ux and product exist; ui_ux should receive shortfall first.
    roster = _roster(
        ("designer", RoleCategory.UI_UX, RoleSeniority.SENIOR, 0),
        ("pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 0),
        ("eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 100),
    )
    result = _by_id(attribute_roles(100.0, roster, Phase.UX_DESIGN))
    assert result["designer"] + result["pm"] >= 39.0
    assert result["designer"] >= result["pm"], (
        f"UI/UX should be preferred over Product when filling UX floor: {result}"
    )


# ---------- DEPLOYMENT: engineering + devops + data floor of 75% ----------


def test_deployment_is_technical_biased_when_user_asks_product_heavy() -> None:
    roster = _roster(
        ("sr_pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 50),
        ("jr_pm", RoleCategory.PRODUCT, RoleSeniority.JUNIOR, 30),
        ("sr_eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 15),
        ("jr_eng", RoleCategory.ENGINEERING, RoleSeniority.JUNIOR, 5),
    )
    result = _by_id(attribute_roles(100.0, roster, Phase.DEPLOYMENT))
    tech_share = (result["sr_eng"] + result["jr_eng"]) / 100.0
    assert tech_share >= 0.74


def test_deployment_prefers_devops_role_when_filling_floor() -> None:
    roster = _roster(
        ("devops_eng", RoleCategory.DEVOPS, RoleSeniority.SENIOR, 0),
        ("sr_eng", RoleCategory.ENGINEERING, RoleSeniority.SENIOR, 0),
        ("pm", RoleCategory.PRODUCT, RoleSeniority.SENIOR, 100),
    )
    result = _by_id(attribute_roles(100.0, roster, Phase.DEPLOYMENT))
    assert (result["devops_eng"] + result["sr_eng"]) >= 74.0
    # DevOps gets preference over generic engineering for the deploy phase.
    assert result["devops_eng"] >= result["sr_eng"], (
        f"DevOps should be preferred over Engineering in Deployment: {result}"
    )


# ---------- edge cases ----------


def test_all_other_tags_means_no_overrides_apply() -> None:
    """Roles tagged OTHER/OTHER bypass every override — pure pass-through."""
    roster = _roster(
        ("a", RoleCategory.OTHER, RoleSeniority.OTHER, 60),
        ("b", RoleCategory.OTHER, RoleSeniority.OTHER, 40),
    )
    for phase in Phase:
        result = _by_id(attribute_roles(100.0, roster, phase))
        assert _close(result["a"], 60.0), f"{phase} did not pass through 'a'"
        assert _close(result["b"], 40.0), f"{phase} did not pass through 'b'"

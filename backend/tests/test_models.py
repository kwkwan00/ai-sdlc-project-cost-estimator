from __future__ import annotations

import pytest

from models.project_schema import CustomRole, RoleRoster
from models.twin_outputs import (
    HourRange,
    RoleCategory,
    RoleHours,
    RoleSeniority,
)


def test_hour_range_pert_mean_weights_most_likely_4x() -> None:
    r = HourRange(optimistic=10, most_likely=20, pessimistic=40)
    # (10 + 4*20 + 40) / 6 = 130 / 6
    assert r.pert_mean == 130 / 6


def test_hour_range_pert_mean_collapses_when_all_equal() -> None:
    r = HourRange(optimistic=15, most_likely=15, pessimistic=15)
    assert r.pert_mean == 15


def test_role_hours_round_trips_through_pydantic_dump() -> None:
    rh = RoleHours(
        role_id="rh1",
        role_description="Sr. Engineer — owns the API surface and reviews PRs",
        category=RoleCategory.ENGINEERING,
        seniority=RoleSeniority.SENIOR,
        hours=42.5,
    )
    dumped = rh.model_dump()
    assert dumped["role_id"] == "rh1"
    assert dumped["category"] == "engineering"
    assert dumped["seniority"] == "senior"
    assert dumped["role_description"].startswith("Sr. Engineer")


def test_custom_role_accepts_a_description_up_to_500_characters() -> None:
    long = "x" * 500
    role = CustomRole(
        role_id="long",
        description=long,
        category=RoleCategory.OTHER,
        seniority=RoleSeniority.OTHER,
        percentage=100,
    )
    assert len(role.description) == 500


def test_custom_role_rejects_description_over_500_characters() -> None:
    with pytest.raises(ValueError):
        CustomRole(
            role_id="long",
            description="x" * 501,
            category=RoleCategory.OTHER,
            seniority=RoleSeniority.OTHER,
            percentage=100,
        )


def test_role_roster_default_descriptions_are_non_empty() -> None:
    roster = RoleRoster.default()
    for r in roster.roles:
        assert r.description.strip(), f"empty description on {r.role_id}"


def test_role_roster_default_sums_to_100_and_has_unique_ids() -> None:
    roster = RoleRoster.default()
    pcts = [r.percentage for r in roster.roles]
    assert sum(pcts) == pytest.approx(100.0, abs=0.5)
    assert len({r.role_id for r in roster.roles}) == len(roster.roles)


def test_role_roster_rejects_duplicate_role_ids() -> None:
    with pytest.raises(ValueError, match="Duplicate role_id"):
        RoleRoster(
            roles=[
                CustomRole(
                    role_id="x",
                    description="A",
                    category=RoleCategory.OTHER,
                    seniority=RoleSeniority.OTHER,
                    percentage=50,
                ),
                CustomRole(
                    role_id="x",
                    description="B",
                    category=RoleCategory.OTHER,
                    seniority=RoleSeniority.OTHER,
                    percentage=50,
                ),
            ]
        )


def test_role_roster_rejects_percentages_that_do_not_sum_to_100() -> None:
    with pytest.raises(ValueError, match="must sum to 100"):
        RoleRoster(
            roles=[
                CustomRole(
                    role_id="a",
                    description="A",
                    category=RoleCategory.OTHER,
                    seniority=RoleSeniority.OTHER,
                    percentage=30,
                ),
                CustomRole(
                    role_id="b",
                    description="B",
                    category=RoleCategory.OTHER,
                    seniority=RoleSeniority.OTHER,
                    percentage=30,
                ),
            ]
        )


def test_role_roster_allows_empty_list_for_explicit_skip() -> None:
    # An empty roster is valid (twins fall back to RoleRoster.default()).
    RoleRoster(roles=[])


def test_role_roster_tolerates_half_point_drift_from_frontend_rounding() -> None:
    # Frontend slider rebalance can land at 99.5 / 100.5 — accept it.
    RoleRoster(
        roles=[
            CustomRole(
                role_id="a",
                description="A",
                category=RoleCategory.OTHER,
                seniority=RoleSeniority.OTHER,
                percentage=49.5,
            ),
            CustomRole(
                role_id="b",
                description="B",
                category=RoleCategory.OTHER,
                seniority=RoleSeniority.OTHER,
                percentage=50.5,
            ),
        ]
    )

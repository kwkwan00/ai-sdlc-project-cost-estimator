"""Shared builders for the SOW tests: a deterministic completed estimate envelope."""

from __future__ import annotations

from datetime import UTC, datetime

from models.project_schema import EstimateEnvelope, EstimateStatus
from models.twin_outputs import (
    Assumption,
    DualScenarioEstimate,
    HourRange,
    Phase,
    PhaseEstimate,
    Risk,
    RoleCategory,
    RoleHeadcount,
    RoleSeniority,
)
from models.wbs_task import WbsTaskInput


def _hr(o: float, m: float, p: float) -> HourRange:
    return HourRange(optimistic=o, most_likely=m, pessimistic=p)


# AI-assisted base labor: eng 1000h×$200=200k, pm 200h×$180=36k → fee-table total $236,000.
# (Distinct from total_cost_ai_assisted_usd=260k, which bakes in Brooks + contingency — the
# SOW deliberately quotes the base labor sum.)
AI_FEE_TOTAL = 236000.0
MANUAL_FEE_TOTAL = 354000.0  # eng 1500×200=300k + pm 300×180=54k


def make_completed_envelope(
    *, method: str = "twins", project_name: str = "Patient Portal Modernization"
) -> EstimateEnvelope:
    roles = [
        RoleHeadcount(
            role_id="eng_senior",
            role_description="Senior Software Engineer",
            category=RoleCategory.ENGINEERING,
            seniority=RoleSeniority.SENIOR,
            headcount=2,
            rate_per_hour=200.0,
            ai_assisted_hours=1000.0,
            manual_only_hours=1500.0,
            ai_assisted_cost_usd=200000.0,
            manual_only_cost_usd=300000.0,
        ),
        RoleHeadcount(
            role_id="pm",
            role_description="Product Manager",
            category=RoleCategory.PRODUCT,
            seniority=RoleSeniority.SENIOR,
            headcount=1,
            rate_per_hour=180.0,
            ai_assisted_hours=200.0,
            manual_only_hours=300.0,
            ai_assisted_cost_usd=36000.0,
            manual_only_cost_usd=54000.0,
        ),
    ]
    phases = [
        PhaseEstimate(
            phase=Phase.DISCOVERY,
            twin_name="discovery_analyst",
            algorithm="UCP",
            ai_assisted_hours=_hr(300, 385, 500),
            manual_only_hours=_hr(320, 397, 520),
            confidence=0.75,
            assumptions=[Assumption(text="Stakeholders are available for discovery workshops.")],
            risks=[
                Risk(
                    description="Scope creep from compliance review",
                    likelihood=0.4,
                    impact_hours_low=50,
                    impact_hours_high=150,
                )
            ],
        ),
        PhaseEstimate(
            phase=Phase.DEVELOPMENT,
            twin_name="development_architect",
            algorithm="COCOMO_II",
            ai_assisted_hours=_hr(700, 880, 1100),
            manual_only_hours=_hr(900, 1100, 1400),
            confidence=0.78,
            assumptions=[Assumption(text="A modern web stack is used throughout.")],
            risks=[
                Risk(
                    description="Integration with the legacy EHR proves more complex than expected",
                    likelihood=0.35,
                    impact_hours_low=100,
                    impact_hours_high=300,
                )
            ],
        ),
    ]
    final = DualScenarioEstimate(
        total_ai_assisted_hours=_hr(1000, 1265, 1600),
        total_manual_only_hours=_hr(1220, 1497, 1920),
        ai_hours_saved_pert=232,
        ai_cost_saved_usd=118000,
        phases=phases,
        confidence=0.76,
        duration_weeks_low=8,
        duration_weeks_high=12,
        headcount_by_role=roles,
        team_size=3,
        optimal_team_size=3,
        total_cost_ai_assisted_usd=260000.0,
        total_cost_manual_only_usd=378000.0,
    )
    kwargs: dict = dict(
        estimate_id="sow-test-1",
        project_name=project_name,
        status=EstimateStatus.COMPLETED,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        final_estimate=final,
    )
    if method == "wbs":
        kwargs["method"] = "wbs"
        kwargs["wbs_tree"] = [
            WbsTaskInput(
                id="t1",
                name="Build feature",
                phase=Phase.DEVELOPMENT,
                role_id="eng_senior",
                optimistic=10,
                most_likely=20,
                pessimistic=30,
            )
        ]
    return EstimateEnvelope(**kwargs)

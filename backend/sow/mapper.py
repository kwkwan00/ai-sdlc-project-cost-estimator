"""Deterministic estimate → SOW-section renderers (no LLM).

Each function turns a completed estimate into the concrete content of one
``source: estimate`` (or the estimate half of a ``hybrid``) section. They are pure over
the envelope + scenario, so they're trivially unit-testable and reproducible.

**Billing basis (deliberate):** the fee table and resource summary quote per-role
*billable labor* — ``hours × rate`` summed across the roster — NOT
``DualScenarioEstimate.total_cost_*_usd`` (which bakes in Brooks coordination overhead and
the contingency reserve). In a time-&-materials SOW those buffers surface as additional
*actual hours billed at the same rates*, not as a separate marked-up contract figure, so
quoting them as the "estimated investment" would misrepresent the rate basis. The fee table
total, the resource summary, and the ``[TOTAL INVESTMENT]`` token therefore all agree
exactly (one number, self-consistent).
"""

from __future__ import annotations

from collections.abc import Callable

from models.project_schema import EstimateEnvelope
from models.twin_outputs import (
    DualScenarioEstimate,
    Phase,
    RoleHeadcount,
)

from .models import Scenario, SowTable

# Human-readable phase labels for the schedule / level-of-effort table.
PHASE_LABELS: dict[str, str] = {
    Phase.DISCOVERY.value: "Discovery & Analysis",
    Phase.UX_DESIGN.value: "UX & Design",
    Phase.DEVELOPMENT.value: "Development",
    Phase.CODE_REVIEW.value: "Code Review",
    Phase.DEPLOYMENT.value: "Deployment & DevOps",
    Phase.QA_TESTING.value: "QA & Testing",
}


def _final(envelope: EstimateEnvelope) -> DualScenarioEstimate:
    """The synthesized estimate. The router only renders completed estimates, so this is
    always present; we assert rather than branch to fail loudly on a contract violation."""
    final = envelope.final_estimate
    assert final is not None, "SOW rendering requires a completed estimate (final_estimate)"
    return final


def _role_hours(hc: RoleHeadcount, scenario: Scenario) -> float:
    return hc.ai_assisted_hours if scenario == "ai_assisted" else hc.manual_only_hours


def _role_cost(hc: RoleHeadcount, scenario: Scenario) -> float:
    return hc.ai_assisted_cost_usd if scenario == "ai_assisted" else hc.manual_only_cost_usd


def money(value: float) -> str:
    return f"${value:,.0f}"


def _hours(value: float) -> str:
    return f"{value:,.0f}"


def total_investment(envelope: EstimateEnvelope, scenario: Scenario) -> float:
    """Sum of per-role billable labor for the scenario (the headline SOW number)."""
    return sum(_role_cost(hc, scenario) for hc in _final(envelope).headcount_by_role)


def fee_table(envelope: EstimateEnvelope, scenario: Scenario) -> SowTable:
    """Section 5 (Fees): one row per staffed role + a total row, T&M billable labor."""
    final = _final(envelope)
    columns = ["Resource", "Hourly Rate", "Estimated Hours", "Estimated Investment"]
    rows: list[list[str]] = []
    total_hours = 0.0
    total_cost = 0.0
    # Stable, readable ordering: largest engagements first.
    staffed = [hc for hc in final.headcount_by_role if _role_hours(hc, scenario) > 0]
    for hc in sorted(staffed, key=lambda h: _role_cost(h, scenario), reverse=True):
        hours = _role_hours(hc, scenario)
        cost = _role_cost(hc, scenario)
        total_hours += hours
        total_cost += cost
        rows.append([hc.role_description, money(hc.rate_per_hour), _hours(hours), money(cost)])
    rows.append(["Total", "", _hours(total_hours), money(total_cost)])
    return SowTable(columns=columns, rows=rows)


def schedule_table(envelope: EstimateEnvelope, scenario: Scenario) -> SowTable:
    """Section 4 (Schedule & Level of Effort): estimated hours per delivery phase."""
    final = _final(envelope)
    columns = ["Phase", "Estimated Hours"]
    rows: list[list[str]] = []
    for phase in final.phases:
        hours_range = phase.ai_assisted_hours if scenario == "ai_assisted" else phase.manual_only_hours
        label = PHASE_LABELS.get(phase.phase.value, phase.phase.value.replace("_", " ").title())
        rows.append([label, _hours(hours_range.most_likely)])
    return SowTable(columns=columns, rows=rows)


def resource_summary(envelope: EstimateEnvelope, scenario: Scenario) -> str:
    """The recurring "time and materials … estimated investment of $X" paragraph."""
    final = _final(envelope)
    investment = total_investment(envelope, scenario)
    low = final.duration_weeks_low
    high = final.duration_weeks_high
    team = final.team_size or len(
        [hc for hc in final.headcount_by_role if _role_hours(hc, scenario) > 0]
    )
    duration = (
        f"approximately {low:.0f}–{high:.0f} weeks"
        if high > low
        else f"approximately {high:.0f} weeks"
    )
    team_clause = f" with a blended team of {team} [COMPANY] consultants" if team else ""
    return (
        "The following resources will be provided by [COMPANY] on a time-and-materials basis "
        f"at an estimated investment of {money(investment)}{team_clause}. "
        f"The engagement is anticipated to complete in {duration}, billed against actual "
        "hours worked."
    )


def assumptions(envelope: EstimateEnvelope, scenario: Scenario) -> list[str]:
    """Project-specific assumption bullets aggregated from the phases (deduped, capped).

    Pulls each phase's assumptions plus its most material risks (rendered as cautionary
    assumptions). The template's standard client-cooperation bullets are prepended by the
    composer; these are the estimate-derived additions.
    """
    final = _final(envelope)
    seen: set[str] = set()
    out: list[str] = []

    def _add(text: str) -> None:
        cleaned = " ".join(text.split())
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)

    for phase in final.phases:
        for a in phase.assumptions:
            _add(a.text)
    # Surface the highest-likelihood risks as forward-looking assumptions so the SOW flags
    # them without the alarming "risk" framing of an internal estimate.
    top_risks = sorted(
        (r for phase in final.phases for r in phase.risks),
        key=lambda r: r.likelihood,
        reverse=True,
    )
    for r in top_risks:
        _add(
            f"The estimate assumes no material expansion from: {r.description} "
            "(additional effort would be handled via change order)."
        )
    return out[:6]


# Closed allow-list the template config validates ``renderer:`` names against. Adding a new
# estimate-driven section means adding its function here.
RENDERERS: dict[str, Callable[[EstimateEnvelope, Scenario], object]] = {
    "fee_table": fee_table,
    "schedule_table": schedule_table,
    "resource_summary": resource_summary,
    "assumptions": assumptions,
}

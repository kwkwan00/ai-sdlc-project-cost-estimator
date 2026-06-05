"""Structured output schemas produced by each twin and assembled into the final estimate.

These Pydantic models double as JSON schemas for Anthropic tool-use, so they MUST stay
JSON-serializable (no arbitrary Python types in fields).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Phase(str, Enum):
    DISCOVERY = "discovery"
    UX_DESIGN = "ux_design"
    DEVELOPMENT = "development"
    CODE_REVIEW = "code_review"
    DEPLOYMENT = "deployment"
    QA_TESTING = "qa_testing"


class RoleCategory(str, Enum):
    """Functional category each user-defined role belongs to.

    Drives the phase-specific overrides in `role_attribution.py`. Default 'other' is
    safe — it just means the role is treated as neutral by every override.
    """

    PRODUCT = "product"
    ENGINEERING = "engineering"
    UI_UX = "ui_ux"
    QA = "qa"
    DEVOPS = "devops"
    DATA = "data"
    OTHER = "other"


class RoleSeniority(str, Enum):
    SENIOR = "senior"
    MID = "mid"
    JUNIOR = "junior"
    OTHER = "other"


class RoleHours(BaseModel):
    """Hours allocated to a single user-defined role for one phase.

    Carries the role's description + tags so downstream code (synthesize, frontend)
    doesn't have to re-resolve them from the roster.
    """

    model_config = ConfigDict(extra="forbid")
    role_id: str = Field(min_length=1, max_length=64)
    role_description: str = Field(min_length=1, max_length=500)
    category: RoleCategory
    seniority: RoleSeniority
    hours: float = Field(ge=0)


class RoleHeadcount(BaseModel):
    """One row in the final estimate's recommended-staffing table."""

    model_config = ConfigDict(extra="forbid")
    role_id: str
    role_description: str
    category: RoleCategory
    seniority: RoleSeniority
    headcount: int = Field(ge=0)


class HourRange(BaseModel):
    """Three-point PERT estimate."""

    model_config = ConfigDict(extra="forbid")
    optimistic: float = Field(ge=0, description="Best-case hours")
    most_likely: float = Field(ge=0, description="Most-likely hours (mid)")
    pessimistic: float = Field(ge=0, description="Worst-case hours")

    @property
    def pert_mean(self) -> float:
        return (self.optimistic + 4 * self.most_likely + self.pessimistic) / 6


class Assumption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    impact_hours: float = Field(
        default=0,
        description="Estimated swing in hours if this assumption is wrong",
    )


class Risk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    likelihood: float = Field(ge=0, le=1, description="0..1 probability")
    impact_hours_low: float = Field(ge=0)
    impact_hours_high: float = Field(ge=0)


class Gap(BaseModel):
    """An information gap surfaced during Pass 1; orchestrator turns these into questions."""

    model_config = ConfigDict(extra="forbid")
    topic: str = Field(description="Short label, e.g. 'integration_count'")
    question_text: str = Field(description="Plain-English question to ask the user")
    impact_hours: float = Field(
        ge=0, description="Rough estimate of how much resolving this would change the estimate"
    )
    suggested_default: str = Field(description="Reasonable default if the user skips")


class PhaseEstimate(BaseModel):
    """Output from a single twin for a single phase. Carries BOTH AI-assisted and manual-only.

    Cost is intentionally NOT computed here — the orchestrator applies the rate table during
    `commercial_processing` so per-twin code stays algorithm-focused.

    `*_role_hours` is a list keyed by the user's role_id (defined in Stage 2's roster);
    each entry self-describes with name + category + seniority so the frontend can render
    the user's labels without re-resolving against the roster.
    """

    model_config = ConfigDict(extra="forbid")
    phase: Phase
    twin_name: str
    algorithm: str = Field(description="e.g. 'UCP', 'COCOMO II', 'TPA'")

    ai_assisted_hours: HourRange
    manual_only_hours: HourRange

    ai_assisted_role_hours: list[RoleHours] = Field(default_factory=list)
    manual_only_role_hours: list[RoleHours] = Field(default_factory=list)

    assumptions: list[Assumption] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)

    confidence: float = Field(ge=0, le=1, description="Twin's self-reported confidence")
    notes: str = Field(default="", description="Free-form reasoning / method notes")


class ClarifyingQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    text: str
    source_phases: list[Phase]
    suggested_default: str
    impact_hours: float = Field(ge=0)
    answered: bool = False
    answer: str | None = None


class DualScenarioEstimate(BaseModel):
    """Final synthesized estimate covering all six phases."""

    model_config = ConfigDict(extra="forbid")
    total_ai_assisted_hours: HourRange
    total_manual_only_hours: HourRange

    ai_hours_saved_pert: float
    ai_cost_saved_usd: float = 0.0

    phases: list[PhaseEstimate]
    confidence: float = Field(ge=0, le=1)
    duration_weeks_low: float
    duration_weeks_high: float

    headcount_by_role: list[RoleHeadcount] = Field(default_factory=list)
    weekly_burn_rate_usd: float = 0.0
    total_cost_ai_assisted_usd: float = 0.0
    total_cost_manual_only_usd: float = 0.0

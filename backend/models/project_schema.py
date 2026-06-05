"""API request/response schemas (Stage 1-5 input + final estimate envelope)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.twin_outputs import (
    ClarifyingQuestion,
    DualScenarioEstimate,
    PhaseEstimate,
    RoleCategory,
    RoleSeniority,
)


class EngagementModel(str, Enum):
    FIXED_PRICE = "fixed_price"
    TIME_AND_MATERIALS = "tm"
    RETAINER = "retainer"
    HYBRID = "hybrid"


class ProjectType(str, Enum):
    GREENFIELD = "greenfield"
    LEGACY_REPLACEMENT = "legacy_replacement"
    ENHANCEMENT = "enhancement"
    INTEGRATION = "integration"
    DATA_MIGRATION = "data_migration"
    AI_ML_BUILD = "ai_ml_build"


class CustomRole(BaseModel):
    """One user-defined resource on the project's roster.

    `role_id` is an opaque, stable string the frontend generates (e.g. a slug or
    nanoid) — it travels into every downstream `RoleHours` entry so the UI can
    correlate them back to the user's row.

    `description` is the free-form text the user writes to describe the role
    (responsibilities, seniority context, anything that helps the LLM/reader
    interpret it). Used as the display label everywhere downstream.

    Tags (`category`, `seniority`) drive the phase-specific overrides in
    `role_attribution.py`. Use `RoleCategory.OTHER` / `RoleSeniority.OTHER` to opt
    out of overrides for a given role.
    """

    model_config = ConfigDict(extra="forbid")
    role_id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    category: RoleCategory = RoleCategory.OTHER
    seniority: RoleSeniority = RoleSeniority.OTHER
    rate_per_hour: float = Field(ge=0, default=0.0)
    percentage: float = Field(ge=0, le=100, default=0.0)


class RoleRoster(BaseModel):
    """The project's resource roster — the single source of truth for who is on the team.

    Carries the rate + effort share + tags. Replaces the old fixed
    `RoleRates` / `RolePercentages` 4-tuple. Validates that role_ids are unique and
    percentages sum to 100 (within 0.5 tolerance for slider rounding).
    """

    model_config = ConfigDict(extra="forbid")
    roles: list[CustomRole] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> RoleRoster:
        if not self.roles:
            return self
        ids = [r.role_id for r in self.roles]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate role_id values in roster")
        total = sum(r.percentage for r in self.roles)
        # Allow 0.5pt drift from frontend slider rounding.
        if abs(total - 100.0) > 0.5:
            raise ValueError(
                f"Role percentages must sum to 100 (got {total:.2f})"
            )
        return self

    @classmethod
    def default(cls) -> RoleRoster:
        """The starter roster the frontend pre-populates if the user doesn't customize.

        Mirrors the old 4-role defaults as tagged custom roles. The role_ids are
        stable so the same defaults round-trip through the wizard without
        renumbering; the description fields are short by default but the user is
        encouraged to expand them with responsibilities and context.
        """
        return cls(
            roles=[
                CustomRole(
                    role_id="sr_product",
                    description="Senior product manager",
                    category=RoleCategory.PRODUCT,
                    seniority=RoleSeniority.SENIOR,
                    rate_per_hour=220.0,
                    percentage=20.0,
                ),
                CustomRole(
                    role_id="jr_product",
                    description="Junior product manager",
                    category=RoleCategory.PRODUCT,
                    seniority=RoleSeniority.JUNIOR,
                    rate_per_hour=140.0,
                    percentage=10.0,
                ),
                CustomRole(
                    role_id="sr_engineer",
                    description="Senior software engineer",
                    category=RoleCategory.ENGINEERING,
                    seniority=RoleSeniority.SENIOR,
                    rate_per_hour=240.0,
                    percentage=50.0,
                ),
                CustomRole(
                    role_id="jr_engineer",
                    description="Junior software engineer",
                    category=RoleCategory.ENGINEERING,
                    seniority=RoleSeniority.JUNIOR,
                    rate_per_hour=150.0,
                    percentage=20.0,
                ),
            ]
        )


class Stage2Context(BaseModel):
    """Subset of planning-outline §4.2 fields included in MVP.

    The team roster lives here (single source of truth for rates + percentages +
    role taxonomy) — Stage 3 only carries the AI maturity sliders now.
    """

    model_config = ConfigDict(extra="forbid")
    industry: str = ""
    project_type: ProjectType = ProjectType.GREENFIELD
    screen_count_estimate: int | None = None
    integration_count: int = 0
    integration_list: list[str] = Field(default_factory=list)
    engagement_model: EngagementModel = EngagementModel.TIME_AND_MATERIALS
    target_timeline_weeks: int | None = None
    regulatory_requirements: list[str] = Field(default_factory=list)
    roster: RoleRoster = Field(default_factory=RoleRoster.default)


class Stage3Maturity(BaseModel):
    """AI maturity per phase (1-5). Defaults to Level 1."""

    model_config = ConfigDict(extra="forbid")
    discovery_maturity: int = Field(default=1, ge=1, le=5)
    ux_design_maturity: int = Field(default=1, ge=1, le=5)
    development_maturity: int = Field(default=1, ge=1, le=5)
    code_review_maturity: int = Field(default=1, ge=1, le=5)
    deployment_maturity: int = Field(default=1, ge=1, le=5)
    qa_testing_maturity: int = Field(default=1, ge=1, le=5)


class CreateEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_name: str | None = None
    raw_input: str = Field(min_length=10, description="Stage 1: unstructured project description")
    stage2: Stage2Context | None = None
    stage3: Stage3Maturity | None = None


class AnswerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, str] = Field(description="question_id -> answer text")
    skip_remaining: bool = False


class EstimateStatus(str, Enum):
    PENDING = "pending"
    PASS_1_RUNNING = "pass_1_running"
    AWAITING_ANSWERS = "awaiting_answers"
    PASS_2_RUNNING = "pass_2_running"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"


class EstimateEnvelope(BaseModel):
    """API response wrapping the in-progress or final estimate."""

    model_config = ConfigDict(extra="forbid")
    estimate_id: str
    project_name: str
    status: EstimateStatus
    created_at: datetime
    pass1_estimates: list[PhaseEstimate] = Field(default_factory=list)
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    pass2_estimates: list[PhaseEstimate] = Field(default_factory=list)
    final_estimate: DualScenarioEstimate | None = None
    error: str | None = None

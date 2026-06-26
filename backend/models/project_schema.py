"""API request/response schemas (Stage 1-5 input + final estimate envelope)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.twin_outputs import (
    ClarifyingQuestion,
    DualScenarioEstimate,
    Phase,
    PhaseEstimate,
    RoleCategory,
    RoleSeniority,
)
from models.wbs_task import WbsTaskInput


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


class CodebaseContext(str, Enum):
    """Where the work lands — the single biggest moderator of realized AI speedup.

    Greenfield captures the most; a large mature codebase the team knows well
    captures the least and can go net-negative (AI's prompt + verification overhead
    exceeds its help — see METR 2025). Unfamiliar large code sits in between: AI
    aids navigation but context-loading taxes it.
    """

    GREENFIELD = "greenfield"
    BROWNFIELD_SMALL = "brownfield_small"
    BROWNFIELD_LARGE_UNFAMILIAR = "brownfield_large_unfamiliar"
    BROWNFIELD_LARGE_FAMILIAR = "brownfield_large_familiar"


class AiToolingLevel(str, Enum):
    """How the team actually applies AI day-to-day — concrete tooling, not an
    abstract 'maturity'. Higher tiers capture more of a task's AI potential but
    carry more review/verification overhead.
    """

    NONE = "none"
    AUTOCOMPLETE = "autocomplete"
    CHAT = "chat"
    AGENTIC = "agentic"


class PhaseToolingLevels(BaseModel):
    """AI tooling level per SDLC phase. Tooling is phase-specific — different tools
    serve different stages (Claude Code for development/review, Figma AI & Claude
    Cowork for UX, Harness.io for deployment, LangSmith for QA), so a team often has
    strong AI assist in one phase and none in another. Each phase defaults to NONE.
    """

    model_config = ConfigDict(extra="forbid")
    discovery: AiToolingLevel = AiToolingLevel.NONE
    ux_design: AiToolingLevel = AiToolingLevel.NONE
    development: AiToolingLevel = AiToolingLevel.NONE
    code_review: AiToolingLevel = AiToolingLevel.NONE
    deployment: AiToolingLevel = AiToolingLevel.NONE
    qa_testing: AiToolingLevel = AiToolingLevel.NONE


class Stage3Context(BaseModel):
    """Stage 3: the factors that drive how much AI accelerates THIS project.

    Replaces the old per-phase "AI maturity" sliders — that scale measured
    org/agent AI maturity, not development acceleration. Realized AI reduction is
    derived per phase from task amenability (LLM-assessed per phase) × codebase
    context × team seniority (from the Stage 2 roster) × that phase's AI tooling,
    minus a verification tax, and may go negative. See `orchestrator/ai_acceleration.py`.
    """

    model_config = ConfigDict(extra="forbid")
    codebase_context: CodebaseContext = CodebaseContext.GREENFIELD
    # Per-phase tooling levels the twins consume. In the Stage 3 wizard these are
    # no longer entered by hand — the classifier agent derives them from
    # `ai_tooling_description` (see backend/tooling_classifier.py). Defaults to NONE
    # so an unclassified / blank description never inflates the AI reduction.
    ai_tooling: PhaseToolingLevels = Field(default_factory=PhaseToolingLevels)
    # The user's freeform description of the AI tools they use (e.g. "Claude Code
    # for dev, CodeRabbit for review, Figma AI for design"). Persisted for audit /
    # transparency; the classified `ai_tooling` above is what drives the estimate.
    ai_tooling_description: str = Field(default="", max_length=2000)
    # Freeform: the technologies the client already uses or proposes (languages,
    # frameworks, cloud, datastores). An ESTIMATION SIGNAL the twins read — a legacy or
    # unfamiliar stack raises effort, a modern/well-supported one can lower it — and the
    # one place the user explicitly names their stack, so the twins (and downstream
    # planners) may reference those specific technologies rather than staying agnostic.
    technology_stack: str = Field(default="", max_length=2000)


class CreateEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_name: str | None = None
    raw_input: str = Field(
        min_length=10,
        max_length=20000,
        description="Stage 1: unstructured project description",
    )
    stage2: Stage2Context | None = None
    stage3: Stage3Context | None = None
    # Phases to estimate. None / omitted ⇒ all six (back-compat). A non-empty list runs exactly
    # those twins; the others are skipped and contribute nothing to hours/cost/headcount/timeline.
    selected_phases: list[Phase] | None = Field(
        default=None,
        description="Subset of SDLC phases to estimate; null ⇒ all six.",
    )

    @model_validator(mode="after")
    def _normalize_selected_phases(self) -> CreateEstimateRequest:
        if self.selected_phases is not None:
            # Order-preserving dedup; an explicit empty list is a client error.
            deduped = list(dict.fromkeys(self.selected_phases))
            if not deduped:
                raise ValueError("selected_phases, when provided, must be non-empty")
            self.selected_phases = deduped
        return self


class AnswerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, str] = Field(description="question_id -> answer text")
    skip_remaining: bool = False

    # Generous caps that a legitimate clarifying-answer set (a handful of questions)
    # never approaches, but that reject an oversized/abusive payload.
    _MAX_ANSWERS = 100
    _MAX_ANSWER_LEN = 5000

    @model_validator(mode="after")
    def _bound_answers(self) -> AnswerSubmission:
        if len(self.answers) > self._MAX_ANSWERS:
            raise ValueError(
                f"Too many answers: {len(self.answers)} (max {self._MAX_ANSWERS})"
            )
        for key, value in self.answers.items():
            if len(value) > self._MAX_ANSWER_LEN:
                raise ValueError(
                    f"Answer for {key!r} is too long: {len(value)} chars "
                    f"(max {self._MAX_ANSWER_LEN})"
                )
        return self


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
    # Which estimation flow produced this envelope. `twins` is the default top-down
    # parametric flow; `wbs` is the bottom-up Work Breakdown Structure flow. Defaulted
    # so persisted pre-WBS envelopes deserialize cleanly.
    method: Literal["twins", "wbs"] = "twins"
    # WBS-only: the finalized task tree + the roster/context it was rolled up with, stored
    # in envelope_json so a completed WBS estimate redisplays its tree and can be DUPLICATED
    # into a new draft without any dependency on Neo4j being up. None on twin estimates.
    wbs_tree: list[WbsTaskInput] | None = None
    wbs_stage2: Stage2Context | None = None
    wbs_stage3: Stage3Context | None = None
    # The Stage-1 project description the WBS was drafted from. Stored so Duplicate can seed the new
    # draft's description (and a later re-draft has prose to plan from). None on twin/pre-field estimates.
    wbs_raw_input: str | None = None

    @model_validator(mode="after")
    def _coherent_method_and_tree(self) -> EstimateEnvelope:
        """Keep ``method`` and ``wbs_tree`` in lockstep so the two flows can't produce a malformed
        envelope: a ``wbs`` estimate must carry its tree (the review page + Duplicate read it back
        from ``envelope_json``), and a ``twins`` estimate must not (a stray tree would render the
        WBS panel for a parametric estimate). The WBS-only context fields ride along with the tree."""
        if self.method == "wbs" and self.wbs_tree is None:
            raise ValueError("method='wbs' requires wbs_tree to be set")
        if self.method == "twins" and self.wbs_tree is not None:
            raise ValueError("method='twins' must not carry a wbs_tree")
        return self

"""Structured output schemas produced by each twin and assembled into the final estimate.

These Pydantic models double as JSON schemas for Anthropic tool-use, so they MUST stay
JSON-serializable (no arbitrary Python types in fields).
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)


def _coerce_json_list(v: Any) -> Any:
    """Tolerate a forced-tool-use field whose array value arrives as a JSON STRING.

    Claude occasionally serializes a list-of-objects tool field (e.g. `risks`) as a
    stringified JSON array instead of a real array. Parse it back into a list so the
    twin doesn't fall back to a stub. Anything else is returned untouched for normal
    validation."""
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):
            return v
        return parsed
    return v


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

    `role_id` MUST reference a `CustomRole.role_id` in the active `RoleRoster`
    (Stage 2). Referential integrity is a convention enforced by
    `role_attribution.attribute_roles` (which only emits roster role_ids) and by the
    aggregation in `synthesize_estimate` — NOT by this model, which has no roster
    context. Do not add roster validation here.
    """

    model_config = ConfigDict(extra="forbid")
    role_id: str = Field(min_length=1, max_length=64)
    role_description: str = Field(min_length=1, max_length=500)
    category: RoleCategory
    seniority: RoleSeniority
    hours: float = Field(ge=0)


class RoleHeadcount(BaseModel):
    """One row in the final estimate's recommended-staffing + cost table."""

    model_config = ConfigDict(extra="forbid")
    role_id: str
    role_description: str
    category: RoleCategory
    seniority: RoleSeniority
    headcount: int = Field(ge=0)
    # Per-staff cost breakdown: this role's total hours across all phases and the
    # resulting labor cost, for each scenario. (hours × rate_per_hour.)
    rate_per_hour: float = Field(default=0.0, ge=0)
    ai_assisted_hours: float = Field(default=0.0, ge=0)
    manual_only_hours: float = Field(default=0.0, ge=0)
    ai_assisted_cost_usd: float = Field(default=0.0, ge=0)
    manual_only_cost_usd: float = Field(default=0.0, ge=0)


class HourRange(BaseModel):
    """Three-point PERT estimate."""

    model_config = ConfigDict(extra="forbid")
    optimistic: float = Field(ge=0, description="Best-case hours (P10 when Monte-Carlo-derived)")
    most_likely: float = Field(ge=0, description="Most-likely hours (deterministic mid / mode)")
    pessimistic: float = Field(ge=0, description="Worst-case hours (P90 when Monte-Carlo-derived)")
    # Monte Carlo extras (None for legacy / stub / pre-MC ranges → backward-compatible
    # with persisted envelopes). `std` is the sample standard deviation of this
    # scenario's hours (used by synthesize_estimate's variance-combine); `percentiles`
    # is the full {p5,p10,p25,p50,p75,p90,p95} vector that feeds the review fan chart.
    std: float | None = Field(default=None, ge=0)
    mean: float | None = Field(default=None, ge=0)
    percentiles: dict[str, float] | None = Field(default=None)

    @model_validator(mode="after")
    def _coerce_pert_ordering(self) -> HourRange:
        """Coerce the three points into a valid PERT ordering instead of raising.

        A malformed LLM range (e.g. optimistic > pessimistic) would silently corrupt
        every downstream PERT mean. We repair it in place: optimistic becomes the
        minimum, pessimistic the maximum, and most_likely is clamped into
        [optimistic, pessimistic]. We coerce rather than raise because this runs inside
        a twin's forced-tool-use validation — a hard raise there crashes the twin, and
        the project values graceful degradation. Already-valid ranges pass through
        untouched.
        """
        o, m, p = self.optimistic, self.most_likely, self.pessimistic
        new_optimistic = min(o, m, p)
        new_pessimistic = max(o, m, p)
        new_most_likely = min(max(m, new_optimistic), new_pessimistic)
        if (
            new_optimistic != o
            or new_pessimistic != p
            or new_most_likely != m
        ):
            logger.warning(
                "HourRange PERT ordering coerced: "
                "(o=%s, m=%s, p=%s) -> (o=%s, m=%s, p=%s)",
                o,
                m,
                p,
                new_optimistic,
                new_most_likely,
                new_pessimistic,
            )
            self.optimistic = new_optimistic
            self.most_likely = new_most_likely
            self.pessimistic = new_pessimistic
        return self

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


class RiskInput(BaseModel):
    """LLM-proposed risk on a twin's `*Inputs` (forced tool-use). Each risk is a
    discrete event the Monte Carlo layer fires with `probability`, adding sampled
    incremental hours in [impact_hours_low, impact_hours_high] to BOTH scenarios.
    Maps 1:1 to the output `Risk` (probability → likelihood). Size impacts as the
    INCREMENTAL hours if the risk materializes; do not also pad the base estimate
    for them (avoid double-counting with EAF / conservative-bias)."""

    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=300)
    probability: float = Field(ge=0, le=1, description="0..1 chance this risk materializes")
    impact_hours_low: float = Field(ge=0, description="Added hours if it fires (low)")
    impact_hours_high: float = Field(ge=0, description="Added hours if it fires (high)")


# The twins' `risks` field type: a list of RiskInput that also tolerates a JSON-string
# array from the LLM (a forced-tool-use quirk) instead of stubbing the whole phase.
RiskInputList = Annotated[list[RiskInput], BeforeValidator(_coerce_json_list)]


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
    # The algorithm's numeric component breakdown (e.g. Fagan: inspection rate, review
    # hours, rework multiplier, tooling hours). Structured so the UI can render it
    # graphically instead of parsing prose. Empty for stub/fallback estimates.
    breakdown: dict[str, float] = Field(default_factory=dict)
    # Realized AI effort reduction applied to this phase, as a percentage (may be
    # negative — AI net-slower). Matches `ai_assisted_hours = manual × (1 - pct/100)`.
    effective_ai_reduction_pct: float = Field(default=0.0)
    notes: str = Field(default="", description="Free-form reasoning notes (prose only)")


class ClarifyingQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    text: str
    source_phases: list[Phase]
    suggested_default: str
    impact_hours: float = Field(ge=0)
    answered: bool = False
    answer: str | None = None


class LlmModelUsage(BaseModel):
    """Token usage + cost for a single model across the estimation run."""

    model_config = ConfigDict(extra="forbid")
    model: str
    calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)


class LlmUsage(BaseModel):
    """Anthropic token usage + estimated $ cost of producing THIS estimate.

    The meta-cost of running the estimator itself (all twin + orchestrator LLM
    calls across Pass 1 and Pass 2), distinct from the project's labor cost.
    """

    model_config = ConfigDict(extra="forbid")
    call_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    by_model: list[LlmModelUsage] = Field(default_factory=list)


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

    # Human-readable warnings surfaced by the consistency_check / synthesize_estimate
    # step (e.g. cross-phase hour anomalies). Populated by the orchestrator; default
    # empty keeps this fully backward-compatible.
    consistency_warnings: list[str] = Field(default_factory=list)

    headcount_by_role: list[RoleHeadcount] = Field(default_factory=list)
    weekly_burn_rate_usd: float = 0.0
    # Team-scaling (Brooks's Law + diminishing returns) outputs — see orchestrator/staffing.py.
    # Defaulted so persisted pre-feature envelopes deserialize cleanly.
    brooks_overhead_pct: float = 0.0      # coordination overhead applied to cost + schedule
    staffing_efficiency_pct: float = 0.0  # realized fraction of ideal linear team scaling
    team_size: int = 0                    # headcount the overhead/efficiency were computed on
    optimal_team_size: int = 0            # the Brooks/diminishing-returns "sweet spot"
    total_cost_ai_assisted_usd: float = 0.0
    total_cost_manual_only_usd: float = 0.0
    # Meta-cost: Anthropic tokens + $ spent producing this estimate. Empty/zero
    # when no LLM calls were captured (e.g. the stub/LLM-free path).
    llm_usage: LlmUsage = Field(default_factory=LlmUsage)

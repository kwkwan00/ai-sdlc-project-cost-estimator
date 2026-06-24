"""Project-context normalization agent for the Stage 2 wizard step.

The user types a free-form description on Stage 1, hits Continue, and the
frontend POSTs that text to `/estimates/draft/prefill`. This module is the
backing **agent**: it makes one forced-tool-use Claude call (the same
`call_structured` plumbing the six estimation twins use) whose response model
constrains every field to the estimator's canonical option set, so the LLM does
the semantic normalization (e.g. "regional clinic" → `healthcare`) and returns
values the Stage 2 form can render directly.

Two layers of normalization, by design:
1. **LLM (primary)** — the response model's fields are the exact enums the form
   renders, so the tool schema constrains Claude to canonical values.
2. **Deterministic tables (backstop)** — `mode="before"` field validators run
   the synonym / coercion tables below over whatever the model emits, so any
   casing or synonym drift ("Healthcare", "medical", "clinic") is mapped to a
   valid option (or "" / empty) rather than leaving the form field blank.

The agent always returns a complete, valid Stage2Context; on any LLM failure
(no API key, network blip) it falls back to empty defaults + a 0.7 ambiguity.
"""

from __future__ import annotations

import logging
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import get_settings
from models.project_schema import (
    EngagementModel,
    ProjectType,
)
from orchestrator.llm import call_structured
from orchestrator.prompts import load_prompt

logger = logging.getLogger(__name__)

# Canonical regulatory labels mirroring the frontend REGULATORY_OPTIONS list.
# The LLM may emit "Hipaa" / "soc 2" / "SOC2" / "pci dss" — normalize all of
# those to the canonical form the frontend renders.
_REGULATORY_NORMALIZATION = {
    "hipaa": "HIPAA",
    "soc 2": "SOC 2",
    "soc2": "SOC 2",
    "soc-2": "SOC 2",
    "pci-dss": "PCI-DSS",
    "pci dss": "PCI-DSS",
    "pci": "PCI-DSS",
    "gdpr": "GDPR",
    "fedramp": "FedRAMP",
    "fed ramp": "FedRAMP",
    "ferpa": "FERPA",
}

# Canonical industry values mirroring the frontend INDUSTRY_OPTIONS dropdown.
# The LLM's industry_hint is free-form, but the Stage 2 <select> can only render
# one of these exact values — an off-list or differently-cased hint (e.g.
# "Healthcare", "medical", "clinic") leaves the field blank. Keep this list in
# sync with frontend/lib/schemas.ts::INDUSTRY_OPTIONS.
_INDUSTRY_OPTIONS = (
    "healthcare",
    "fintech",
    "insurance",
    "retail",
    "manufacturing",
    "government",
    "education",
    "media",
    "telecom",
    "other",
)

# Synonyms / adjacent phrasings the LLM commonly emits, mapped to the canonical
# option. Keys are matched lowercased; canonical values are matched directly via
# _INDUSTRY_OPTIONS so they don't need entries here.
_INDUSTRY_SYNONYMS = {
    "health care": "healthcare",
    "healthtech": "healthcare",
    "health tech": "healthcare",
    "medical": "healthcare",
    "medicine": "healthcare",
    "clinic": "healthcare",
    "clinical": "healthcare",
    "hospital": "healthcare",
    "pharma": "healthcare",
    "pharmaceutical": "healthcare",
    "pharmaceuticals": "healthcare",
    "life sciences": "healthcare",
    "biotech": "healthcare",
    "finance": "fintech",
    "financial": "fintech",
    "financial services": "fintech",
    "banking": "fintech",
    "bank": "fintech",
    "payments": "fintech",
    "lending": "fintech",
    "insurtech": "insurance",
    "insurer": "insurance",
    "ecommerce": "retail",
    "e-commerce": "retail",
    "commerce": "retail",
    "shopping": "retail",
    "consumer goods": "retail",
    "industrial": "manufacturing",
    "factory": "manufacturing",
    "logistics": "manufacturing",
    "supply chain": "manufacturing",
    "gov": "government",
    "govtech": "government",
    "public sector": "government",
    "civic": "government",
    "edtech": "education",
    "ed tech": "education",
    "education technology": "education",
    "school": "education",
    "university": "education",
    "e-learning": "education",
    "elearning": "education",
    "entertainment": "media",
    "streaming": "media",
    "publishing": "media",
    "news": "media",
    "gaming": "media",
    "telecommunications": "telecom",
    "telecommunication": "telecom",
    "telephony": "telecom",
    "telco": "telecom",
}


def _normalize_industry(hint: str) -> str:
    """Map a free-form LLM industry hint to a canonical INDUSTRY_OPTIONS value.

    Returns "" when there's no confident match so the Stage 2 <select> stays on
    its "— pick one —" placeholder for the user to choose, rather than carrying
    an off-list value the dropdown can't render. Tries, in order: case-folded
    exact match against the canonical list, a synonym-table lookup, then a
    word-level scan so phrases like "regional clinic" still resolve.
    """
    cleaned = hint.strip().lower()
    if not cleaned:
        return ""
    if cleaned in _INDUSTRY_OPTIONS:
        return cleaned
    if cleaned in _INDUSTRY_SYNONYMS:
        return _INDUSTRY_SYNONYMS[cleaned]
    # Fall back to scanning individual words against both maps so a descriptive
    # hint ("regional clinic", "consumer banking app") still resolves.
    words = cleaned.replace("/", " ").replace("-", " ").split()
    for word in words:
        if word in _INDUSTRY_OPTIONS:
            return word
        if word in _INDUSTRY_SYNONYMS:
            return _INDUSTRY_SYNONYMS[word]
    return ""


class DraftPrefillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_input: str = Field(
        min_length=10,
        description="The Stage 1 project description to analyze.",
    )


class Stage2Fields(BaseModel):
    """The Stage 2 project-context fields the prefill agent infers.

    Deliberately a SUBSET of Stage2Context: it omits the team `roster` entirely.
    The roster is proposed asynchronously by the AG-UI roster agent on Stage 2
    (see roster_agui.py); the prefill endpoint is fully roster-free, and the
    frontend supplies its own default roster until the AG-UI snapshot lands.
    Mirror Stage2Context's non-roster fields here if that model changes.
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


class Stage2Prefill(BaseModel):
    """Response shape for POST /estimates/draft/prefill.

    `stage2` carries the LLM-inferred Stage 2 fields (industry, project type,
    screens, integrations, regulatory) — but NOT the team roster, which is
    proposed asynchronously by the AG-UI roster agent on Stage 2. `summary` and
    `ambiguity_score` let the UI echo how the LLM interpreted the description and
    warn when the input was too vague for a confident extraction.
    """

    model_config = ConfigDict(extra="forbid")
    stage2: Stage2Fields
    summary: str = Field(default="")
    ambiguity_score: float = Field(default=0.5, ge=0, le=1)
    # AI tools the description mentions, for pre-filling the Stage 3 tooling field.
    # Empty string when none were named.
    ai_tooling_description: str = Field(default="")


def _normalize_regulatory(mentions: list[str]) -> list[str]:
    """Map raw mentions to the canonical labels the frontend's chips render."""
    seen: list[str] = []
    for raw in mentions:
        canonical = _REGULATORY_NORMALIZATION.get(raw.lower().strip())
        if canonical and canonical not in seen:
            seen.append(canonical)
    return seen


def _coerce_project_type(hint: str) -> ProjectType:
    """ProjectType is a string enum; coerce or fall back to greenfield."""
    try:
        return ProjectType(hint.strip().lower())
    except ValueError:
        return ProjectType.GREENFIELD


class IndustryOption(str, Enum):
    """Canonical Stage 2 industry values (mirrors frontend INDUSTRY_OPTIONS).

    UNKNOWN ("") lets the agent decline to classify — the Stage 2 <select> then
    stays on its "— pick one —" placeholder for the user to choose.
    """

    HEALTHCARE = "healthcare"
    FINTECH = "fintech"
    INSURANCE = "insurance"
    RETAIL = "retail"
    MANUFACTURING = "manufacturing"
    GOVERNMENT = "government"
    EDUCATION = "education"
    MEDIA = "media"
    TELECOM = "telecom"
    OTHER = "other"
    UNKNOWN = ""


class RegulatoryRequirement(str, Enum):
    """Canonical compliance regimes (mirrors frontend REGULATORY_OPTIONS)."""

    HIPAA = "HIPAA"
    SOC2 = "SOC 2"
    PCI_DSS = "PCI-DSS"
    GDPR = "GDPR"
    FEDRAMP = "FedRAMP"
    FERPA = "FERPA"


class NormalizedProjectContext(BaseModel):
    """The prefill agent's structured output — Stage 2 values already normalized.

    Field types are the exact enums the Stage 2 form renders, so the forced
    tool-use schema constrains Claude to emit only valid option values (the LLM
    does the semantic mapping). The `mode="before"` validators apply the
    deterministic synonym / coercion tables as a backstop, absorbing any casing
    or synonym drift before enum validation so the call never hard-fails on an
    off-list value — it degrades to the empty / default option instead.
    """

    model_config = ConfigDict(extra="forbid")

    industry: IndustryOption = IndustryOption.UNKNOWN
    project_type: ProjectType = ProjectType.GREENFIELD
    screen_count_estimate: int = Field(default=0, ge=0)
    integrations: list[str] = Field(default_factory=list)
    regulatory_requirements: list[RegulatoryRequirement] = Field(default_factory=list)
    summary: str = Field(default="")
    ambiguity_score: float = Field(default=0.5, ge=0, le=1)
    # Any AI development tools the description explicitly mentions, as a short phrase
    # the Stage 3 tooling field can be pre-filled with (e.g. "Claude Code for dev,
    # CodeRabbit for review"). Empty when the description names no AI tools.
    ai_tooling_description: str = Field(default="", max_length=2000)

    @field_validator("industry", mode="before")
    @classmethod
    def _backstop_industry(cls, v: object) -> str:
        # _normalize_industry returns a canonical option or "" — both valid
        # IndustryOption values, so this never raises on an off-list hint.
        return _normalize_industry(str(getattr(v, "value", v)))

    @field_validator("project_type", mode="before")
    @classmethod
    def _backstop_project_type(cls, v: object) -> ProjectType:
        return _coerce_project_type(str(getattr(v, "value", v)))

    @field_validator("regulatory_requirements", mode="before")
    @classmethod
    def _backstop_regulatory(cls, v: object) -> list[str]:
        if isinstance(v, str):
            items: list[object] = [v]
        elif isinstance(v, (list, tuple)):
            items = list(v)
        else:
            return []
        return _normalize_regulatory([str(getattr(x, "value", x)) for x in items])


async def run_prefill_agent(raw_input: str) -> NormalizedProjectContext:
    """The prefill agent: one forced-tool-use call that returns normalized values.

    Mirrors the twin pattern — load_prompt + call_structured against a Pydantic
    response model. Raises if the LLM call fails (no API key, network); callers
    handle the fallback.
    """
    system = load_prompt("prefill_agent")
    user = (
        f"Project description:\n\n{raw_input}\n\n"
        "Extract and normalize the Stage 2 project context."
    )
    logger.debug(
        "running prefill agent (model=%s, input_chars=%d)",
        get_settings().anthropic_model_prefill,
        len(raw_input),
    )
    return await call_structured(
        system=system,
        user=user,
        response_model=NormalizedProjectContext,
        tool_name="normalize_project_context",
        # Prefill is bounded extract-and-normalize with an enum schema + backstop
        # validators — Haiku is the cheap/fast fit. Pinned so it doesn't inherit
        # the heavyweight ANTHROPIC_MODEL the six estimation twins use.
        model=get_settings().anthropic_model_prefill,
    )


def _normalized_to_fields(n: NormalizedProjectContext) -> Stage2Fields:
    """Map the agent's normalized output into the roster-free Stage2Fields.

    engagement_model and target_timeline_weeks aren't inferred from a description
    — they stay at defaults for the user to set. The roster is omitted entirely
    (proposed later via AG-UI; the frontend supplies its own default meanwhile).
    """
    integrations = list(n.integrations)
    screens = n.screen_count_estimate if n.screen_count_estimate > 0 else None
    return Stage2Fields(
        industry=n.industry.value,
        project_type=n.project_type,
        screen_count_estimate=screens,
        integration_count=len(integrations),
        # Cap so a runaway extraction can't dump 200 entries into the form.
        integration_list=integrations[:20],
        engagement_model=EngagementModel.TIME_AND_MATERIALS,
        target_timeline_weeks=None,
        regulatory_requirements=[r.value for r in n.regulatory_requirements],
    )


async def prefill_stage2_from_raw(raw_input: str) -> Stage2Prefill:
    """Top-level entry point used by the HTTP endpoint.

    Runs the prefill (interpretation) agent and maps its normalized output into
    the roster-free Stage2Fields. The team roster is NOT part of this response —
    it's proposed asynchronously by the AG-UI roster agent on Stage 2, and the
    frontend supplies its own default until then. On any LLM failure (missing
    ANTHROPIC_API_KEY, network blip) it degrades to empty Stage 2 fields + a
    conservative 0.7 ambiguity score, so the response is always a valid
    Stage2Prefill the form can consume.
    """
    try:
        normalized = await run_prefill_agent(raw_input)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "prefill agent failed (%s); returning empty Stage 2 fields. "
            "Set ANTHROPIC_API_KEY for real prefill.",
            exc,
        )
        return Stage2Prefill(
            stage2=Stage2Fields(),
            summary=raw_input[:280],
            ambiguity_score=0.7,
        )
    logger.info(
        "prefill complete (industry=%s, project_type=%s, ambiguity=%.2f)",
        normalized.industry.value or "unknown",
        normalized.project_type.value,
        normalized.ambiguity_score,
    )
    return Stage2Prefill(
        stage2=_normalized_to_fields(normalized),
        summary=normalized.summary,
        ambiguity_score=normalized.ambiguity_score,
        ai_tooling_description=normalized.ai_tooling_description,
    )

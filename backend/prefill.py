"""LLM-driven prefill for the Stage 2 (Project context) wizard step.

The user types a free-form description on Stage 1, hits Continue, and the
frontend POSTs that text to `/estimates/draft/prefill`. This module is the
backing logic — it routes through `extract_context_from_raw` (same Claude
tool-use path that `parse_input` uses during Pass 1) and then maps the
LLM-extracted ParsedContext into a Stage2Context the form can pre-populate
fields from.

Conservative mapping: every field falls back to its Stage2Context default when
the LLM didn't extract a usable value, so the prefill response is always a
complete, valid Stage2Context.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from models.project_schema import (
    EngagementModel,
    ProjectType,
    RoleRoster,
    Stage2Context,
)
from orchestrator.nodes.parse_input import (
    ParsedContext,
    extract_context_from_raw,
)

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


class DraftPrefillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_input: str = Field(
        min_length=10,
        description="The Stage 1 project description to analyze.",
    )


class Stage2Prefill(BaseModel):
    """Response shape for POST /estimates/draft/prefill.

    `stage2` is a complete Stage2Context with LLM-inferred values where possible
    and class defaults elsewhere — the frontend can hand it straight into the
    Stage 2 form's default values. `summary` and `ambiguity_score` are exposed
    so the UI can echo how the LLM interpreted the description and warn when the
    input was too vague for a confident extraction.
    """

    model_config = ConfigDict(extra="forbid")
    stage2: Stage2Context
    summary: str = Field(default="")
    ambiguity_score: float = Field(default=0.5, ge=0, le=1)


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


def parsed_context_to_stage2(parsed: ParsedContext) -> Stage2Context:
    """Map the LLM-extracted ParsedContext into a Stage2Context partial.

    Fields NOT inferable from a free-form description (engagement_model,
    target_timeline_weeks, role roster) are left at Stage2Context defaults so
    the user fills them in deliberately.
    """
    integrations = list(parsed.integration_mentions)
    screens = parsed.screen_count_estimate if parsed.screen_count_estimate > 0 else None

    return Stage2Context(
        industry=parsed.industry_hint.strip(),
        project_type=_coerce_project_type(parsed.project_type_hint),
        screen_count_estimate=screens,
        integration_count=len(integrations),
        # Cap so a runaway extraction can't dump 200 entries into the form.
        integration_list=integrations[:20],
        engagement_model=EngagementModel.TIME_AND_MATERIALS,
        target_timeline_weeks=None,
        regulatory_requirements=_normalize_regulatory(parsed.regulatory_mentions),
        roster=RoleRoster.default(),
    )


async def prefill_stage2_from_raw(raw_input: str) -> Stage2Prefill:
    """Top-level entry point used by the HTTP endpoint.

    Reuses `extract_context_from_raw` so the LLM prompt and graceful-fallback
    semantics live in one place. When the LLM call fails (no API key, network
    blip), the underlying helper returns a minimal ParsedContext with just the
    raw summary + a 0.7 ambiguity score, so the response shape is still valid
    and the form gets defaults instead of an error.
    """
    parsed = await extract_context_from_raw(raw_input)
    return Stage2Prefill(
        stage2=parsed_context_to_stage2(parsed),
        summary=parsed.summary,
        ambiguity_score=parsed.ambiguity_score,
    )

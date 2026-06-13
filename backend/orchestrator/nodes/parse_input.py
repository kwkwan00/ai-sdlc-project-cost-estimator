"""parse_input — extracts structured signals from the raw text input.

For MVP: ask Claude to extract a small set of features (industry, project_type, screen
hints, integration mentions, regulatory mentions, tech stack hints). Output becomes
the `parsed_context` field on EstimationState and is injected into every twin's prompt.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from db.repositories import get_calibration_for_all_phases, get_reduction_bands
from models.estimation_state import EstimationState
from observability.langfuse_wrapper import traced
from orchestrator.llm import call_structured

logger = logging.getLogger(__name__)


class ParsedContext(BaseModel):
    """Structured signals extracted from raw input."""

    model_config = ConfigDict(extra="forbid")
    industry_hint: str = Field(default="", description="e.g. 'healthcare', 'fintech', 'retail'")
    project_type_hint: str = Field(
        default="greenfield",
        description="One of: greenfield, legacy_replacement, enhancement, integration, data_migration, ai_ml_build",
    )
    screen_count_estimate: int = Field(default=0, ge=0)
    integration_mentions: list[str] = Field(default_factory=list)
    regulatory_mentions: list[str] = Field(default_factory=list)
    tech_stack_mentions: list[str] = Field(default_factory=list)
    user_role_mentions: list[str] = Field(default_factory=list)
    ai_feature_mentions: list[str] = Field(default_factory=list)
    summary: str = Field(default="", description="One-paragraph plain-English summary of the scope")
    ambiguity_score: float = Field(
        default=0.5,
        ge=0,
        le=1,
        description="How ambiguous is the input? 1.0 = highly ambiguous, 0.0 = fully specified",
    )


SYSTEM = """You are the intake analyst for a software project cost estimator.

Read the user's raw project description and extract structured signals. Be conservative:
when something is not stated, leave the field empty / 0 rather than guessing. The
downstream twin agents will surface gaps and ask follow-up questions.
"""


def _fallback_context(raw_input: str) -> dict:
    """Minimal context derived without an LLM. Used when ANTHROPIC_API_KEY is
    missing or the call fails — lets the graph still complete with stub twins."""
    return ParsedContext(
        summary=raw_input[:280],
        ambiguity_score=0.7,
    ).model_dump()


async def _load_calibration(state: EstimationState) -> list[dict]:
    """Pull historical aggregates from Postgres so twins can anchor their estimates.

    The repository call returns [] when Postgres is disabled — that's the same
    shape as "cold start" (no history yet), which the twins already handle.
    Flatten the per-phase dict into a single list because EstimationState.
    calibration_examples is declared as `list[dict]`.
    """
    stage2 = state.get("stage2")
    stage3 = state.get("stage3")
    parsed_hint = state.get("parsed_context", {}) or {}
    industry = (
        (stage2.industry if stage2 else "")
        or parsed_hint.get("industry_hint", "")
    ) or None
    project_type = (
        (stage2.project_type.value if stage2 else None)
        or parsed_hint.get("project_type_hint")
    )
    try:
        by_phase = await get_calibration_for_all_phases(
            industry=industry, project_type=project_type, stage3=stage3
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Calibration fetch failed (%s); twins will run uncalibrated", exc)
        return []
    flat: list[dict] = []
    for rows in by_phase.values():
        flat.extend(rows)
    return flat


async def _load_reduction_bands() -> dict:
    """DB-tunable per-(phase, tooling) reduction guardrail bands → graph state.

    Returns {} when Postgres is disabled/unreachable, so twins fall back to the
    in-code ``DEFAULT_BANDS`` in orchestrator/ai_acceleration.py.
    """
    try:
        return await get_reduction_bands()
    except Exception as exc:  # noqa: BLE001
        logger.warning("reduction-band fetch failed (%s); twins will use code defaults", exc)
        return {}


@traced(name="parse_input")
async def parse_input(state: EstimationState) -> dict:
    # Short-circuit when an external caller has pre-populated parsed_context
    # (used by the smoke harness and tests so they can skip the LLM call).
    existing = state.get("parsed_context") or {}
    if existing:
        calibration = await _load_calibration(state)
        bands = await _load_reduction_bands()
        logger.debug("parse_input: pre-populated context; %d calibration row(s) loaded", len(calibration))
        return {
            "parsed_context": existing,
            "calibration_examples": calibration,
            "reduction_bands": bands,
        }

    parsed = await extract_context_from_raw(state["raw_input"])
    parsed_dict = parsed.model_dump()
    logger.info(
        "parse_input complete: industry=%r project_type=%r screens=%d integrations=%d regulatory=%d ambiguity=%.2f",
        parsed.industry_hint,
        parsed.project_type_hint,
        parsed.screen_count_estimate,
        len(parsed.integration_mentions),
        len(parsed.regulatory_mentions),
        parsed.ambiguity_score,
    )

    # Calibration runs after parse so Stage-2-less requests still benefit from
    # the LLM-extracted industry / project_type hints.
    state_after = {**state, "parsed_context": parsed_dict}
    calibration = await _load_calibration(state_after)  # type: ignore[arg-type]
    bands = await _load_reduction_bands()
    logger.debug("parse_input: %d calibration row(s) loaded", len(calibration))
    return {
        "parsed_context": parsed_dict,
        "calibration_examples": calibration,
        "reduction_bands": bands,
    }


async def extract_context_from_raw(raw_input: str) -> ParsedContext:
    """Run Claude over the raw project description and return a ParsedContext.

    Falls back to a minimal `_fallback_context` shape (summary + ambiguity only)
    when the LLM call fails for any reason (missing API key, network error,
    parsing failure). Both the graph's `parse_input` node and the
    `POST /estimates/draft/prefill` HTTP endpoint route through this helper so
    the extraction prompt and fallback semantics stay in one place.
    """
    user_prompt = f"Project description:\n\n{raw_input}\n\nExtract structured signals."
    try:
        return await call_structured(
            system=SYSTEM,
            user=user_prompt,
            response_model=ParsedContext,
            tool_name="extract_context",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "extract_context_from_raw failed (%s); using fallback context. "
            "Set ANTHROPIC_API_KEY for real parsing.",
            exc,
        )
        return ParsedContext.model_validate(_fallback_context(raw_input))

"""Pre-submission draft endpoints backing the Stage 2 / Stage 3 wizard.

These run before an estimate id exists: LLM prefill of the Stage 2 context, the
Stage 3 AI-tooling classifier, and the AG-UI team-roster proposal run. All three are
best-effort — the underlying helpers degrade to safe defaults rather than surfacing
LLM/MCP errors to the user.
"""

from __future__ import annotations

from fastapi import APIRouter

from prefill import DraftPrefillRequest, Stage2Prefill, prefill_stage2_from_raw
from roster_agui import roster_agui_endpoint
from tooling_classifier import (
    ClassifyToolingRequest,
    ToolingClassification,
    classify_ai_tooling,
)

router = APIRouter(prefix="/estimates/draft", tags=["drafts"])


@router.post("/prefill", response_model=Stage2Prefill)
async def prefill_stage2(req: DraftPrefillRequest) -> Stage2Prefill:
    """LLM-driven prefill for the Stage 2 wizard step.

    Runs Claude over the raw Stage 1 description and returns a Stage2Context
    the frontend can hand to the form as default values. Always returns a
    valid response — the underlying extractor falls back to a minimal context
    (defaults + low confidence) when the LLM call fails, so the endpoint never
    surfaces an API-key or network error to the user.
    """
    return await prefill_stage2_from_raw(req.raw_input)


@router.post("/classify-tooling", response_model=ToolingClassification)
async def classify_tooling(req: ClassifyToolingRequest) -> ToolingClassification:
    """Classify the freeform Stage 3 AI-tooling description into per-phase levels.

    The frontend sends the user's free-text tooling description on Stage 3 submit;
    this maps it to the six `AiToolingLevel`s the twins consume. Always returns a
    valid result — a blank description or any LLM/MCP failure degrades to all-'none'
    (no AI acceleration), so the endpoint never surfaces an error to the user.
    Tools the model can't identify are researched via the self-hosted docs-mcp-server;
    if that's unavailable, those tools stay 'none'.
    """
    return await classify_ai_tooling(req.description)


# AG-UI agent-run endpoint for the team-roster proposal (Option B). The frontend
# fires this from Stage 2 (after prefill) and applies the streamed STATE_SNAPSHOT
# roster to the form. Registered via add_api_route so the handler's RunAgentInput
# body + Request signature drives FastAPI parsing.
router.add_api_route("/roster/agui", roster_agui_endpoint, methods=["POST"])

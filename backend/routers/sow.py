"""Statement of Work (SOW) export endpoints.

Two-step, generation separated from rendering so the user edits between them:

* ``POST /estimates/{id}/sow``        → generate a resolved ``SowDocument`` (one LLM call) +
                                        its generation meta-cost. The editable preview source.
* ``POST /estimates/{id}/sow/docx``   → render a (possibly edited) ``SowDocument`` to a
                                        downloadable ``.docx`` (pure; no LLM).

Both work for twins and WBS estimates (both carry a ``final_estimate``). Generation requires
a COMPLETED estimate.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Response

import runtime
from models.project_schema import EstimateStatus
from orchestrator.usage import bind_usage_accumulator, summarize_usage
from sow.composer import build_sow_document
from sow.config import SowTemplateError, load_sow_template
from sow.models import Scenario, SowDocxRequest, SowGenerateResponse
from sow.renderer import render_docx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sow"])

_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _safe_filename(project_name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9 _-]", "", project_name or "estimate")
    base = re.sub(r"\s+", " ", base).strip()[:80].strip() or "estimate"
    return f"{base} - SOW.docx"


@router.post("/estimates/{estimate_id}/sow", response_model=SowGenerateResponse)
async def generate_sow(estimate_id: str, scenario: Scenario = "ai_assisted") -> SowGenerateResponse:
    """Generate a Statement of Work from a completed estimate.

    ``scenario`` selects which cost scenario drives the fee table / resource summary
    (default the AI-assisted delivery cost). 400 unless the estimate is COMPLETED.
    """
    env = await runtime.resolve_envelope(estimate_id)  # in-memory → Postgres fallback
    if env is None:
        raise HTTPException(status_code=404, detail="Estimate not found")
    if env.status != EstimateStatus.COMPLETED or env.final_estimate is None:
        raise HTTPException(
            status_code=400,
            detail=f"Estimate is not completed (status={env.status.value}); cannot generate a SOW.",
        )

    # Capture the SOW agent's token cost for this one call (separate from the estimate's).
    acc: list[dict] = []
    bind_usage_accumulator(acc)
    document = await build_sow_document(env, scenario)
    usage = summarize_usage(acc)
    logger.info(
        "SOW generated (estimate=%s, scenario=%s, unresolved=%d, cost=$%.4f)",
        estimate_id,
        scenario,
        len(document.placeholders),
        usage.cost_usd,
    )
    return SowGenerateResponse(document=document, llm_usage=usage)


@router.post("/estimates/{estimate_id}/sow/docx")
async def download_sow_docx(estimate_id: str, body: SowDocxRequest) -> Response:
    """Render a (possibly edited) ``SowDocument`` to a downloadable ``.docx``. No LLM."""
    document = body.document
    try:
        template = (
            load_sow_template(document.template_id)
            if document.template_id
            else load_sow_template()
        )
    except SowTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data = render_docx(document, template)
    return Response(
        content=data,
        media_type=_DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(document.project_name)}"'},
    )

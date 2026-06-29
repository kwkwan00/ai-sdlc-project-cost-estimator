"""Per-leaf 3-point hour estimator — the **#5c** single-leaf assist for the WBS editor.

The planner drafts the whole tree; this re-estimates **one** leaf on demand (the editor's "Suggest
hours" button) from the project brief + the leaf's place in the tree (its work package + sibling
tasks, with their hours) so the number stays proportionate to the rest of the WBS. Far cheaper than
re-running the whole planner.

**Retrieval-augmented**: it also pulls the realized 3-point hours of the most similar tasks from PAST
estimates out of Qdrant (`nearest_wbs_tasks`, scoped to the leaf's phase) and feeds them as the
strongest calibration anchor. That retrieval is best-effort — when Qdrant / embeddings are
unavailable it returns nothing and the estimator falls back to brief + siblings + context.

One forced-tool `call_structured` (same `config.wbs_model` + `wbs_reasoning_effort` as the planner, so
a suggested estimate matches how the tree was drafted). Degrades to **no suggestion**
(`available=False`) when the leaf isn't found / on blank input / any LLM failure / no API key — never
raises, so the endpoint always returns.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from config import get_settings
from models.twin_outputs import PHASE_LABELS
from models.validators import clip_text, coerce_pert_ordering
from models.wbs_schema import WbsLeafHoursRequest, WbsLeafHoursSuggestion
from models.wbs_task import WbsTaskInput
from orchestrator.calibration_index import nearest_wbs_tasks, wbs_task_text
from orchestrator.llm import call_structured
from orchestrator.prompts import load_prompt

# How many similar past tasks to retrieve from Qdrant as calibration anchors (best-effort RAG).
_SIMILAR_K = 5

logger = logging.getLogger(__name__)

# One tiny estimate, but `max`-effort reasoning shares the output-token envelope, so leave generous
# room (still a fraction of the planner's budget, well under the non-streaming ~21k ceiling).
_MAX_TOKENS = 8192


class _LeafHoursReply(BaseModel):
    """The estimator's forced-tool output (no `llm_usage` — the endpoint attaches that)."""

    model_config = ConfigDict(extra="forbid")
    optimistic: float = Field(default=0.0, ge=0)
    most_likely: float = Field(default=0.0, ge=0)
    pessimistic: float = Field(default=0.0, ge=0)
    rationale: Annotated[str, BeforeValidator(clip_text(400))] = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _order(self) -> _LeafHoursReply:
        self.optimistic, self.most_likely, self.pessimistic = coerce_pert_ordering(
            self.optimistic, self.most_likely, self.pessimistic
        )
        return self


def _find_leaf_context(
    tree: list[WbsTaskInput], leaf_id: str
) -> tuple[WbsTaskInput | None, str, list[dict[str, object]]]:
    """Locate the target leaf and the context that keeps its estimate proportionate: the name of the
    work package it lives under and its sibling leaf tasks (name + current most_likely hours). Returns
    ``(None, "", [])`` when the id isn't a leaf in the tree."""

    def _walk(nodes: list[WbsTaskInput], parent_name: str) -> tuple[WbsTaskInput | None, str, list[dict[str, object]]]:
        for node in nodes:
            if node.id == leaf_id and node.is_leaf:
                siblings = [
                    {"name": s.name, "most_likely_hours": s.most_likely or 0.0}
                    for s in nodes
                    if s.id != node.id and s.is_leaf
                ]
                return node, parent_name, siblings
            if node.children:
                found = _walk(node.children, node.name)
                if found[0] is not None:
                    return found
        return None, "", []

    return _walk(tree, "")


async def _similar_past_tasks(leaf: WbsTaskInput) -> list[dict[str, object]]:
    """Retrieve the nearest past WBS leaf tasks (with their realized 3-point hours) from Qdrant, scoped
    to this leaf's phase — the RAG calibration anchors. Best-effort: returns ``[]`` when Qdrant /
    embeddings are unavailable (`nearest_wbs_tasks` never raises), so the estimator works without it."""
    hits = await nearest_wbs_tasks(wbs_task_text(leaf), phase=leaf.phase, limit=_SIMILAR_K)
    return [
        {
            "name": h.get("task_name", ""),
            "optimistic": h.get("optimistic", 0.0),
            "most_likely": h.get("most_likely", 0.0),
            "pessimistic": h.get("pessimistic", 0.0),
            "similarity": round(float(h.get("score", 0.0)), 3),
        }
        for h in hits
    ]


def _build_user_prompt(
    req: WbsLeafHoursRequest,
    leaf: WbsTaskInput,
    package_name: str,
    siblings: list[dict[str, object]],
    similar_past_tasks: list[dict[str, object]],
) -> str:
    stage2 = req.stage2
    context = {
        "project_brief": req.raw_input.strip() or "(none provided)",
        "industry": stage2.industry if stage2 else "",
        "project_type": stage2.project_type.value if stage2 else "",
        "regulatory": stage2.regulatory_requirements if stage2 else [],
        "integrations": stage2.integration_list if stage2 else [],
        "codebase_context": req.stage3.codebase_context.value if req.stage3 else "",
        "work_package": package_name,
        "target_task": {
            "name": leaf.name,
            "description": leaf.description,
            "phase": PHASE_LABELS[leaf.phase] if leaf.phase else "",
            "current_estimate": {
                "optimistic": leaf.optimistic or 0.0,
                "most_likely": leaf.most_likely or 0.0,
                "pessimistic": leaf.pessimistic or 0.0,
            },
        },
        "sibling_tasks_for_proportion": siblings,
        # Realized 3-point hours of the most similar tasks from PAST estimates (empty when the vector
        # store has no match / is unavailable) — the strongest calibration anchor when present.
        "similar_past_tasks": similar_past_tasks,
    }
    return (
        "Estimate three-point hours for the target_task only, proportionate to its siblings and "
        "anchored to similar_past_tasks when present.\n\n"
        + json.dumps(context, indent=2)
    )


async def suggest_leaf_hours(req: WbsLeafHoursRequest) -> WbsLeafHoursSuggestion:
    """Suggest a 3-point estimate for one leaf. Always returns — `available=False` when the leaf isn't
    found or any failure (the endpoint wraps the result with `llm_usage`)."""
    leaf, package_name, siblings = _find_leaf_context(req.tree, req.leaf_id)
    if leaf is None:
        logger.info("suggest_leaf_hours: leaf %r not found in tree; no suggestion", req.leaf_id)
        return WbsLeafHoursSuggestion()
    # RAG: ground the estimate in the realized hours of similar past tasks (no-op without Qdrant).
    similar = await _similar_past_tasks(leaf)
    try:
        settings = get_settings()
        reply = await call_structured(
            system=load_prompt("wbs_leaf_estimator"),
            user=_build_user_prompt(req, leaf, package_name, siblings, similar),
            response_model=_LeafHoursReply,
            tool_name="suggest_leaf_hours",
            model=settings.wbs_model,
            effort=settings.wbs_reasoning_effort,
            max_tokens=_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "leaf hours estimator failed (%s); no suggestion. Set ANTHROPIC_API_KEY to run it.", exc
        )
        return WbsLeafHoursSuggestion()
    return WbsLeafHoursSuggestion(
        available=True,
        optimistic=reply.optimistic,
        most_likely=reply.most_likely,
        pessimistic=reply.pessimistic,
        rationale=reply.rationale,
    )

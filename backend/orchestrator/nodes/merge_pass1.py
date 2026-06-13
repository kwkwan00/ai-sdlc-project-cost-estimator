"""merge_pass1 — deduplicate Pass-1 gaps into 5-10 clarifying questions ranked by impact.

Two-layer dedup:
1. Deterministic exact-topic collapse (`_dedupe_gaps`) — always runs, no network.
2. LLM semantic consolidation (`_consolidate_semantically`) — clusters near-duplicate
   questions the six twins raised independently (same fact, different wording). It
   degrades to the layer-1 result on any failure (no API key, bad output, error), so
   the node never hard-fails and stays runnable without Anthropic access.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, ConfigDict, Field

from config import get_settings
from models.estimation_state import EstimationState
from models.twin_outputs import ClarifyingQuestion, Gap, Phase
from observability.langfuse_wrapper import traced
from orchestrator.llm import call_structured

from ._twin_base import load_prompt

logger = logging.getLogger(__name__)

_MAX_QUESTIONS = 10
_MIN_QUESTIONS = 0  # zero is OK if every twin had no gaps
# Only worth an LLM round-trip when there are enough candidates to plausibly overlap.
_MIN_CANDIDATES_FOR_LLM = 3


def _dedupe_gaps(
    pass1: list,
) -> list[tuple[Gap, list[Phase]]]:
    """Collapse gaps with the same topic; track which phases surfaced them."""
    by_topic: dict[str, tuple[Gap, list[Phase]]] = {}
    for phase_estimate in pass1:
        for gap in phase_estimate.gaps:
            key = gap.topic.strip().lower()
            if key in by_topic:
                existing_gap, phases = by_topic[key]
                phases.append(phase_estimate.phase)
                # Keep the gap with the higher impact_hours.
                if gap.impact_hours > existing_gap.impact_hours:
                    by_topic[key] = (gap, phases)
            else:
                by_topic[key] = (gap, [phase_estimate.phase])
    return list(by_topic.values())


class _GapCluster(BaseModel):
    """One group of candidate questions a single user answer would resolve."""

    model_config = ConfigDict(extra="forbid")

    member_indices: list[int] = Field(
        description="Indices of candidate questions in this cluster", min_length=1
    )
    merged_question: str = Field(
        description="One plain-English question covering every sub-ask in the cluster"
    )


class _ConsolidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clusters: list[_GapCluster] = Field(default_factory=list)


def _merge_cluster(
    members: list[tuple[Gap, list[Phase]]], merged_question: str
) -> tuple[Gap, list[Phase]]:
    """Fold a cluster of (gap, phases) into a single representative entry.

    Keeps the highest-impact member's magnitude + default, unions the phases, and
    rewrites the question text only when more than one candidate is being merged.
    """
    base_gap, _ = max(members, key=lambda m: m[0].impact_hours)
    phases: list[Phase] = []
    for _, member_phases in members:
        for p in member_phases:
            if p not in phases:
                phases.append(p)
    text = base_gap.question_text if len(members) == 1 else merged_question.strip()
    merged = Gap(
        topic=base_gap.topic,
        question_text=text or base_gap.question_text,
        impact_hours=base_gap.impact_hours,
        suggested_default=base_gap.suggested_default,
    )
    return merged, phases


def _validate_partition(clusters: list[_GapCluster], n: int) -> bool:
    """True iff the clusters form an exact partition of indices 0..n-1."""
    seen: set[int] = set()
    for cluster in clusters:
        for idx in cluster.member_indices:
            if idx < 0 or idx >= n or idx in seen:
                return False
            seen.add(idx)
    return len(seen) == n


async def _consolidate_semantically(
    candidates: list[tuple[Gap, list[Phase]]],
) -> list[tuple[Gap, list[Phase]]]:
    """LLM pass that clusters near-duplicate questions; returns `candidates` on failure."""
    if len(candidates) < _MIN_CANDIDATES_FOR_LLM:
        return candidates
    if not get_settings().anthropic_api_key:
        return candidates  # keep the node runnable without Anthropic access

    listing = "\n".join(
        f"{i}. [topic: {gap.topic}] {gap.question_text}"
        for i, (gap, _) in enumerate(candidates)
    )
    try:
        result = await call_structured(
            system=load_prompt("question_consolidator"),
            user=f"Candidate questions:\n{listing}",
            response_model=_ConsolidationResult,
            tool_name="submit_clusters",
            model=get_settings().anthropic_model_merge,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("question consolidation failed (%s); using topic-dedup only", exc)
        return candidates

    if not _validate_partition(result.clusters, len(candidates)):
        logger.warning(
            "question consolidation returned a non-partition (%d clusters for %d "
            "candidates); using topic-dedup only",
            len(result.clusters),
            len(candidates),
        )
        return candidates

    consolidated = [
        _merge_cluster([candidates[i] for i in cluster.member_indices], cluster.merged_question)
        for cluster in result.clusters
    ]
    logger.info(
        "question consolidation: %d candidate(s) -> %d cluster(s)",
        len(candidates),
        len(consolidated),
    )
    return consolidated


@traced(name="merge_pass1")
async def merge_pass1(state: EstimationState) -> dict:
    pass1 = state.get("pass1_estimates", [])
    grouped = _dedupe_gaps(pass1)
    grouped = await _consolidate_semantically(grouped)
    grouped.sort(key=lambda item: item[0].impact_hours, reverse=True)

    questions: list[ClarifyingQuestion] = []
    for gap, phases in grouped[:_MAX_QUESTIONS]:
        questions.append(
            ClarifyingQuestion(
                id=str(uuid.uuid4()),
                text=gap.question_text,
                source_phases=phases,
                suggested_default=gap.suggested_default,
                impact_hours=gap.impact_hours,
            )
        )
    logger.info(
        "merge_pass1 complete: %d phase estimate(s), %d unique gap(s) -> %d clarifying question(s)",
        len(pass1),
        len(grouped),
        len(questions),
    )
    return {"clarifying_questions": questions}

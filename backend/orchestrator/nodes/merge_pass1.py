"""merge_pass1 — deduplicate Pass-1 gaps into 5-10 clarifying questions ranked by impact."""

from __future__ import annotations

import logging
import uuid

from models.estimation_state import EstimationState
from models.twin_outputs import ClarifyingQuestion, Gap, Phase
from observability.langfuse_wrapper import traced

logger = logging.getLogger(__name__)

_MAX_QUESTIONS = 10
_MIN_QUESTIONS = 0  # zero is OK if every twin had no gaps


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


@traced(name="merge_pass1")
async def merge_pass1(state: EstimationState) -> dict:
    pass1 = state.get("pass1_estimates", [])
    grouped = _dedupe_gaps(pass1)
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

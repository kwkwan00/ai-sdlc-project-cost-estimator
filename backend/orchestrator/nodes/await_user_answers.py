"""await_user_answers — LangGraph interrupt; the orchestrator pauses here until the
client POSTs Stage 4 answers and the graph is resumed with Command(resume=...).
"""

from __future__ import annotations

import logging

from langgraph.types import interrupt

from models.estimation_state import EstimationState
from models.twin_outputs import ClarifyingQuestion
from observability.langfuse_wrapper import traced

logger = logging.getLogger(__name__)


@traced(name="await_user_answers")
async def await_user_answers(state: EstimationState) -> dict:
    questions: list[ClarifyingQuestion] = state.get("clarifying_questions", [])

    # NOTE: this log must precede interrupt() — interrupt() suspends the node.
    logger.info("await_user_answers: awaiting user answers (%d clarifying question(s))", len(questions))

    # `interrupt(value)` pauses the graph. The next `invoke(Command(resume=...))`
    # call returns the resume payload here.
    resume_payload = interrupt(
        {
            "type": "clarifying_questions",
            "questions": [q.model_dump() for q in questions],
        }
    )

    # Expected resume shape: {"answers": {question_id: answer_text}, "skip_remaining": bool}
    answers: dict[str, str] = (resume_payload or {}).get("answers", {})
    logger.info("await_user_answers: resumed with %d answer(s)", len(answers))

    annotated: list[ClarifyingQuestion] = []
    for q in questions:
        if q.id in answers:
            annotated.append(q.model_copy(update={"answered": True, "answer": answers[q.id]}))
        else:
            annotated.append(
                q.model_copy(update={"answered": True, "answer": q.suggested_default})
            )

    return {"user_answers": answers, "clarifying_questions": annotated}

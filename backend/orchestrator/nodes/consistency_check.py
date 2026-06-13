"""consistency_check — flag contradictions across twin outputs.

MVP: emits warnings into the synthesized estimate's `notes` but does NOT trigger
selective re-estimation. That's a Phase 4 feature.
"""

from __future__ import annotations

import logging

from models.estimation_state import EstimationState
from models.twin_outputs import Phase, PhaseEstimate
from observability.langfuse_wrapper import traced

logger = logging.getLogger(__name__)


def _capers_jones_qa_ratio_warning(pass2: list[PhaseEstimate]) -> str | None:
    """QA effort should typically be 30-40% of total. Flag if way outside that band."""
    by_phase = {pe.phase: pe.ai_assisted_hours.pert_mean for pe in pass2}
    qa = by_phase.get(Phase.QA_TESTING, 0.0)
    total = sum(by_phase.values())
    if total <= 0:
        return None
    ratio = qa / total
    if ratio < 0.15:
        return f"QA share is only {ratio:.0%} of total effort; Capers Jones ratio suggests 30-40%."
    if ratio > 0.55:
        return f"QA share is {ratio:.0%} of total effort; unusually high for a typical project."
    return None


@traced(name="consistency_check")
async def consistency_check(state: EstimationState) -> dict:
    pass2 = state.get("pass2_estimates", [])
    warnings: list[str] = []

    if (w := _capers_jones_qa_ratio_warning(pass2)) is not None:
        warnings.append(w)

    logger.info(
        "consistency_check complete: %d phase(s), %d warning(s)", len(pass2), len(warnings)
    )
    return {"consistency_warnings": warnings}

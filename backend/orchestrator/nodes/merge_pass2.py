"""merge_pass2 — pure fan-in; the `operator.add` reducer on pass2_estimates already
collects all six twin outputs. This node exists to give the consistency_check node
a single deterministic predecessor.
"""

from __future__ import annotations

from models.estimation_state import EstimationState
from observability.langfuse_wrapper import traced


@traced(name="merge_pass2")
async def merge_pass2(state: EstimationState) -> dict:
    # No-op; the reducer has already aggregated.
    _ = state.get("pass2_estimates", [])
    return {}

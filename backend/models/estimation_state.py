"""LangGraph EstimationState — passes through every node in the two-pass cycle."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from models.project_schema import Stage2Context, Stage3Maturity
from models.twin_outputs import (
    ClarifyingQuestion,
    DualScenarioEstimate,
    PhaseEstimate,
)


class EstimationState(TypedDict, total=False):
    """State shape for the orchestrator graph.

    Fields with `operator.add` reducers can be written to by parallel nodes
    (the six twin fan-outs) without conflict. Single-writer fields use no reducer.
    """

    estimate_id: str
    project_name: str
    raw_input: str
    stage2: Stage2Context | None
    stage3: Stage3Maturity

    parsed_context: dict
    calibration_examples: list[dict]

    pass1_estimates: Annotated[list[PhaseEstimate], operator.add]
    clarifying_questions: list[ClarifyingQuestion]
    user_answers: dict[str, str]

    pass2_estimates: Annotated[list[PhaseEstimate], operator.add]
    final_estimate: DualScenarioEstimate | None

    error: str | None

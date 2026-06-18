"""LangGraph EstimationState — passes through every node in the two-pass cycle."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from models.project_schema import Stage2Context, Stage3Context
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
    stage3: Stage3Context

    parsed_context: dict
    calibration_examples: list[dict]
    # DB-loaded per-(phase, tooling) reduction guardrail bands, nested
    # {phase_value: {tooling_value: [lo, hi]}}. Empty → twins use code defaults.
    reduction_bands: dict
    # Selected discovery sizing algorithm: "ucp" (default) | "function_points".
    discovery_sizing_method: str
    # Selected development sizing algorithm: "cocomo" (default) | "function_points" | "cosmic_function_points".
    development_sizing_method: str
    # Selected QA/testing sizing algorithm: "tpa" (default) | "test_case_point" | "defect_removal".
    qa_sizing_method: str
    # Global contingency management-reserve %, uplifting final cost + timeline (0 = none).
    contingency_pct: float

    pass1_estimates: Annotated[list[PhaseEstimate], operator.add]
    clarifying_questions: list[ClarifyingQuestion]
    user_answers: dict[str, str]

    pass2_estimates: Annotated[list[PhaseEstimate], operator.add]
    final_estimate: DualScenarioEstimate | None

    # Inter-node results (single-writer; written post-fan-out, so no reducer).
    # `consistency_check` emits warnings consumed by `synthesize_estimate`.
    consistency_warnings: list[str]
    # `commercial_processing` prices the roster; consumed by `synthesize_estimate`.
    total_cost_ai_assisted_usd: float
    total_cost_manual_only_usd: float

    error: str | None

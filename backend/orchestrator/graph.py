"""StateGraph wiring per planning outline §3.3.

Topology:
  START
    → parse_input
    → [discovery_p1, ux_p1, dev_p1, code_review_p1, deployment_p1, qa_p1]   (fan-out)
    → merge_pass1
    → await_user_answers (LangGraph interrupt — Stage 4)
    → [discovery_p2, ux_p2, dev_p2, code_review_p2, deployment_p2, qa_p2]   (fan-out)
    → merge_pass2
    → consistency_check
    → commercial_processing
    → synthesize_estimate
    → END
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from db.neo4j_adapter import make_checkpointer
from models.estimation_state import EstimationState
from orchestrator.nodes.await_user_answers import await_user_answers
from orchestrator.nodes.code_review_sentinel import code_review_pass1, code_review_pass2
from orchestrator.nodes.commercial_processing import commercial_processing
from orchestrator.nodes.consistency_check import consistency_check
from orchestrator.nodes.deployment_devops import deployment_pass1, deployment_pass2
from orchestrator.nodes.development_architect import development_pass1, development_pass2
from orchestrator.nodes.discovery_analyst import (
    discovery_analyst_pass1,
    discovery_analyst_pass2,
)
from orchestrator.nodes.merge_pass1 import merge_pass1
from orchestrator.nodes.merge_pass2 import merge_pass2
from orchestrator.nodes.parse_input import parse_input
from orchestrator.nodes.qa_testing_strategist import qa_testing_pass1, qa_testing_pass2
from orchestrator.nodes.synthesize_estimate import synthesize_estimate
from orchestrator.nodes.ux_design_strategist import ux_design_pass1, ux_design_pass2

logger = logging.getLogger(__name__)

_PASS1_TWINS = [
    ("discovery_p1", discovery_analyst_pass1),
    ("ux_p1", ux_design_pass1),
    ("dev_p1", development_pass1),
    ("code_review_p1", code_review_pass1),
    ("deployment_p1", deployment_pass1),
    ("qa_p1", qa_testing_pass1),
]

_PASS2_TWINS = [
    ("discovery_p2", discovery_analyst_pass2),
    ("ux_p2", ux_design_pass2),
    ("dev_p2", development_pass2),
    ("code_review_p2", code_review_pass2),
    ("deployment_p2", deployment_pass2),
    ("qa_p2", qa_testing_pass2),
]


def build_graph(*, with_checkpointer: bool = True):
    """Build and compile the orchestrator graph.

    Set `with_checkpointer=False` for unit tests that don't need interrupt/resume.
    """
    g = StateGraph(EstimationState)

    g.add_node("parse_input", parse_input)
    for name, fn in _PASS1_TWINS:
        g.add_node(name, fn)
    g.add_node("merge_pass1", merge_pass1)
    g.add_node("await_user_answers", await_user_answers)
    for name, fn in _PASS2_TWINS:
        g.add_node(name, fn)
    g.add_node("merge_pass2", merge_pass2)
    g.add_node("consistency_check", consistency_check)
    g.add_node("commercial_processing", commercial_processing)
    g.add_node("synthesize_estimate", synthesize_estimate)

    g.add_edge(START, "parse_input")

    # Pass 1 fan-out.
    for name, _ in _PASS1_TWINS:
        g.add_edge("parse_input", name)
        g.add_edge(name, "merge_pass1")

    g.add_edge("merge_pass1", "await_user_answers")

    # Pass 2 fan-out.
    for name, _ in _PASS2_TWINS:
        g.add_edge("await_user_answers", name)
        g.add_edge(name, "merge_pass2")

    g.add_edge("merge_pass2", "consistency_check")
    g.add_edge("consistency_check", "commercial_processing")
    g.add_edge("commercial_processing", "synthesize_estimate")
    g.add_edge("synthesize_estimate", END)

    if with_checkpointer:
        checkpointer = make_checkpointer()
        compiled = g.compile(checkpointer=checkpointer)
        logger.info(
            "orchestrator graph compiled (with_checkpointer=%s, checkpointer=%s)",
            True,
            type(checkpointer).__name__,
        )
        return compiled
    compiled = g.compile()
    logger.info("orchestrator graph compiled (with_checkpointer=%s)", False)
    return compiled

"""Offline, deterministic tests for the synthetic-project simulator.

These run with NO ANTHROPIC_API_KEY and make NO LLM calls — the generator is pure
stdlib ``random``. They pin: (1) determinism in ``(n, seed)``, (2) that the gold
actuals equal each twin's OWN ``compute_*`` on the TRUE sampled inputs with the TRUE
effective AI reduction, (3) that every generated ``EvalCase`` is well-formed and folds
into the right agent bucket, and (4) the load-bearing property the harness relies on:
a twin fed the TRUE inputs produces a Monte-Carlo band that BRACKETS the gold.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import pytest

from evals.models import EvalCase
from evals.synthetic import (
    _phase_gold,
    _proposed_reduction_for,
    _TrueProject,
    generate_cases,
    generate_cases_by_agent,
)
from models.twin_outputs import Phase
from orchestrator.ai_acceleration import effective_ai_reduction
from orchestrator.montecarlo import make_rng
from orchestrator.nodes import (
    code_review_sentinel,
    deployment_devops,
    development_architect,
    discovery_analyst,
    qa_testing_strategist,
    ux_design_strategist,
)
from orchestrator.nodes._twin_base import make_reduction_sampler
from orchestrator.nodes.code_review_sentinel import compute_review_hours
from orchestrator.nodes.deployment_devops import compute_cmp_hours
from orchestrator.nodes.development_architect import compute_cocomo_hours
from orchestrator.nodes.discovery_analyst import compute_ucp_hours
from orchestrator.nodes.qa_testing_strategist import compute_qa_hours
from orchestrator.nodes.ux_design_strategist import compute_scp_hours

_ALL_PHASES = list(Phase)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_generate_cases_is_deterministic() -> None:
    a = generate_cases(4, seed=42)
    b = generate_cases(4, seed=42)
    assert [c.model_dump(mode="json") for c in a] == [c.model_dump(mode="json") for c in b]


def test_generate_cases_seed_changes_output() -> None:
    a = generate_cases(4, seed=42)
    c = generate_cases(4, seed=43)
    # Same positional ids, but the sampled inputs (and thus gold) differ.
    assert [x.id for x in a] == [x.id for x in c]
    assert [x.input for x in a] != [x.input for x in c]


def test_generate_cases_shape() -> None:
    cases = generate_cases(3, seed=1)
    assert len(cases) == 18  # 6 phases x 3 projects
    agents = {c.agent for c in cases}
    assert agents == {
        "discovery",
        "ux_design",
        "development",
        "code_review",
        "deployment",
        "qa_testing",
    }
    # Every case validates and carries the four gold keys.
    for c in cases:
        assert isinstance(c, EvalCase)
        assert set(c.gold) >= {"actual_manual_ml", "actual_ai_ml", "target_manual_ml", "target_ai_ml"}
        assert c.gold["actual_manual_ml"] > 0
        assert c.gold["actual_ai_ml"] > 0


def test_generate_cases_empty_for_nonpositive_n() -> None:
    assert generate_cases(0, seed=1) == []
    assert generate_cases(-3, seed=1) == []


def test_generate_cases_by_agent_groups_correctly() -> None:
    grouped = generate_cases_by_agent(5, seed=2)
    assert set(grouped) == {
        "discovery",
        "ux_design",
        "development",
        "code_review",
        "deployment",
        "qa_testing",
    }
    assert all(len(v) == 5 for v in grouped.values())


# --------------------------------------------------------------------------- #
# Gold == compute_*(true_inputs) with the TRUE reduction
# --------------------------------------------------------------------------- #

_ComputeFn = Callable[[Any], tuple[float, dict]]
_COMPUTE_BY_PHASE: dict[Phase, tuple[str, _ComputeFn]] = {
    Phase.DISCOVERY: ("discovery_inputs", compute_ucp_hours),
    Phase.UX_DESIGN: ("ux_inputs", compute_scp_hours),
    Phase.DEVELOPMENT: ("dev_inputs", compute_cocomo_hours),
    Phase.CODE_REVIEW: ("review_inputs", compute_review_hours),
    Phase.DEPLOYMENT: ("deployment_inputs", compute_cmp_hours),
    Phase.QA_TESTING: ("qa_inputs", compute_qa_hours),
}


@pytest.mark.parametrize("phase", _ALL_PHASES)
def test_gold_equals_compute_on_true_inputs(phase: Phase) -> None:
    # Rebuild a project from a fresh RNG (same sequence the generator consumes) and
    # confirm the phase gold == compute_*(true_inputs) + true effective reduction.
    rng = random.Random(99)
    proj = _TrueProject(0, rng)

    attr, compute = _COMPUTE_BY_PHASE[phase]
    inputs = getattr(proj, attr)
    expected_manual = compute(inputs)[0]
    eff = effective_ai_reduction(
        phase=phase,
        tooling=proj.tooling[phase],
        codebase=proj.codebase,
        roster=proj.roster,
        proposed_reduction=_proposed_reduction_for(proj, phase),
        regulated=proj.regulated,
    )
    expected_ai = expected_manual * (1.0 - eff)

    gm, ga = _phase_gold(proj, phase)
    assert gm == pytest.approx(round(expected_manual, 2))
    assert ga == pytest.approx(round(expected_ai, 2))


def test_case_gold_matches_phase_gold() -> None:
    # The gold written onto each EvalCase must equal _phase_gold for that project.
    rng = random.Random(7)
    proj = _TrueProject(0, rng)
    expected = {phase: _phase_gold(proj, phase) for phase in _ALL_PHASES}

    cases = generate_cases(1, seed=7)
    by_agent = {c.agent: c for c in cases}
    name = {
        Phase.DISCOVERY: "discovery",
        Phase.UX_DESIGN: "ux_design",
        Phase.DEVELOPMENT: "development",
        Phase.CODE_REVIEW: "code_review",
        Phase.DEPLOYMENT: "deployment",
        Phase.QA_TESTING: "qa_testing",
    }
    for phase, (gm, ga) in expected.items():
        case = by_agent[name[phase]]
        assert case.gold["actual_manual_ml"] == pytest.approx(gm)
        assert case.gold["actual_ai_ml"] == pytest.approx(ga)


# --------------------------------------------------------------------------- #
# The MC band brackets the gold (interval_calibration would score 1.0)
# --------------------------------------------------------------------------- #

_MODULE_BY_PHASE = {
    Phase.DISCOVERY: (discovery_analyst, "discovery_inputs"),
    Phase.UX_DESIGN: (ux_design_strategist, "ux_inputs"),
    Phase.DEVELOPMENT: (development_architect, "dev_inputs"),
    Phase.CODE_REVIEW: (code_review_sentinel, "review_inputs"),
    Phase.DEPLOYMENT: (deployment_devops, "deployment_inputs"),
    Phase.QA_TESTING: (qa_testing_strategist, "qa_inputs"),
}


@pytest.mark.parametrize("phase", _ALL_PHASES)
def test_true_inputs_produce_band_bracketing_gold(phase: Phase) -> None:
    rng_seed = random.Random(123)
    # Advance through a few projects to exercise varied tooling/codebase draws.
    projects = [_TrueProject(i, rng_seed) for i in range(6)]
    module, attr = _MODULE_BY_PHASE[phase]

    for proj in projects:
        gm, ga = _phase_gold(proj, phase)
        eff = effective_ai_reduction(
            phase=phase,
            tooling=proj.tooling[phase],
            codebase=proj.codebase,
            roster=proj.roster,
            proposed_reduction=_proposed_reduction_for(proj, phase),
            regulated=proj.regulated,
        )
        reduction_ctx = {
            "phase": phase,
            "codebase": proj.codebase,
            "tooling": proj.tooling[phase],
            "roster": proj.roster,
            "regulated": proj.regulated,
            "bands": None,
        }
        sampler = make_reduction_sampler(
            reduction_ctx=reduction_ctx,
            proposed_point=_proposed_reduction_for(proj, phase),
            reduction_range=None,
        )
        est = module.build_phase_estimate(
            getattr(proj, attr),
            effective_reduction=eff,
            roster=proj.roster,
            rng=make_rng(f"synthtest:{proj.idx}:{phase.value}"),
            reduction_sampler=sampler,
        )
        # most_likely is the deterministic mid == gold by construction (the gold is
        # rounded to 2 decimals, so allow a sub-0.01h rounding gap).
        assert est.manual_only_hours.most_likely == pytest.approx(gm, abs=0.01)
        # And the MC band brackets the gold actuals.
        assert est.manual_only_hours.optimistic - 0.01 <= gm <= est.manual_only_hours.pessimistic + 0.01
        assert est.ai_assisted_hours.optimistic - 0.01 <= ga <= est.ai_assisted_hours.pessimistic + 0.01

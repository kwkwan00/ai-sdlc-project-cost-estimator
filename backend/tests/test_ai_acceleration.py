"""Unit coverage for the AI-acceleration model (orchestrator/ai_acceleration.py).

Per-(phase × tooling) reduction is a guardrail band [lo, hi]; the LLM's proposed
reduction is clamped into it (midpoint when there's no proposal), then moderated by
codebase context and team seniority, with penalties that can go negative.
"""

from __future__ import annotations

from models.project_schema import AiToolingLevel as T
from models.project_schema import CodebaseContext as C
from models.project_schema import CustomRole, RoleRoster
from models.twin_outputs import Phase, RoleCategory
from models.twin_outputs import RoleSeniority as S
from orchestrator.ai_acceleration import (
    DEFAULT_BANDS,
    NEGATIVE_FLOOR,
    band_for,
    effective_ai_reduction,
    seniority_factor,
)


def _roster(*seniorities: S) -> RoleRoster:
    n = len(seniorities)
    each = round(100.0 / n, 2)
    roles, acc = [], 0.0
    for i, sen in enumerate(seniorities):
        pct = each if i < n - 1 else round(100.0 - acc, 2)
        acc += each
        roles.append(
            CustomRole(
                role_id=f"r{i}",
                description="role",
                category=RoleCategory.ENGINEERING,
                seniority=sen,
                rate_per_hour=200.0,
                percentage=pct,
            )
        )
    return RoleRoster(roles=roles)


def test_band_for_defaults_and_override() -> None:
    assert band_for(Phase.DEVELOPMENT, T.AGENTIC) == DEFAULT_BANDS[(Phase.DEVELOPMENT, T.AGENTIC)]
    overrides = {"development": {"agentic": [0.30, 0.40]}}
    assert band_for(Phase.DEVELOPMENT, T.AGENTIC, overrides) == (0.30, 0.40)
    # An override for a different cell doesn't affect this one → falls back to default.
    assert band_for(Phase.QA_TESTING, T.CHAT, overrides) == DEFAULT_BANDS[(Phase.QA_TESTING, T.CHAT)]


def test_no_tooling_means_no_reduction() -> None:
    assert (
        effective_ai_reduction(
            phase=Phase.DEVELOPMENT,
            tooling=T.NONE,
            codebase=C.GREENFIELD,
            roster=RoleRoster.default(),
            proposed_reduction=0.5,
        )
        == 0.0
    )


def test_autocomplete_does_not_apply_to_non_coding_phases() -> None:
    # Discovery, UX design, and code review have no AUTOCOMPLETE band → zero reduction
    # even with an eager proposal (inline tab-completion doesn't help those phases).
    for phase in (Phase.DISCOVERY, Phase.UX_DESIGN, Phase.CODE_REVIEW):
        assert (phase, T.AUTOCOMPLETE) not in DEFAULT_BANDS
        assert (
            effective_ai_reduction(
                phase=phase,
                tooling=T.AUTOCOMPLETE,
                codebase=C.GREENFIELD,
                roster=RoleRoster.default(),
                proposed_reduction=0.5,
            )
            == 0.0
        )
    # ...but it still applies to code-writing phases.
    assert (Phase.DEVELOPMENT, T.AUTOCOMPLETE) in DEFAULT_BANDS


def test_proposed_reduction_is_clamped_into_band_and_moderated() -> None:
    lo, hi = DEFAULT_BANDS[(Phase.DEVELOPMENT, T.AGENTIC)]
    # An over-eager LLM proposal (0.9) is clamped to hi, then moderated down.
    eff = effective_ai_reduction(
        phase=Phase.DEVELOPMENT,
        tooling=T.AGENTIC,
        codebase=C.GREENFIELD,
        roster=_roster(S.MID, S.MID),
        proposed_reduction=0.9,
    )
    assert 0.0 < eff <= hi  # never exceeds the band ceiling


def test_no_proposal_uses_band_midpoint() -> None:
    lo, hi = DEFAULT_BANDS[(Phase.DISCOVERY, T.CHAT)]
    mid = (lo + hi) / 2
    eff = effective_ai_reduction(
        phase=Phase.DISCOVERY,
        tooling=T.CHAT,
        codebase=C.GREENFIELD,
        roster=_roster(S.MID, S.MID),  # seniority_factor ≈ 1.0, no penalty
        proposed_reduction=None,
    )
    assert eff == round(mid, 10) or abs(eff - mid) < 0.02


def test_codebase_moderation_lowers_familiar_brownfield() -> None:
    common = dict(
        phase=Phase.DEVELOPMENT, tooling=T.AGENTIC, roster=_roster(S.MID), proposed_reduction=0.18
    )
    green = effective_ai_reduction(codebase=C.GREENFIELD, **common)
    familiar = effective_ai_reduction(codebase=C.BROWNFIELD_LARGE_FAMILIAR, **common)
    assert green > familiar


def test_familiar_brownfield_senior_regulated_goes_negative() -> None:
    eff = effective_ai_reduction(
        phase=Phase.DEVELOPMENT,
        tooling=T.AGENTIC,
        codebase=C.BROWNFIELD_LARGE_FAMILIAR,
        roster=_roster(S.SENIOR, S.SENIOR),
        proposed_reduction=0.20,
        regulated=True,
    )
    assert eff < 0.0
    assert eff >= NEGATIVE_FLOOR


def test_seniority_factor_is_inverse_and_bounded() -> None:
    assert seniority_factor(_roster(S.JUNIOR)) > seniority_factor(_roster(S.SENIOR))
    assert seniority_factor(None) == 1.0
    assert seniority_factor(RoleRoster()) == 1.0
    assert 0.6 <= seniority_factor(_roster(S.SENIOR)) <= 1.25

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

# Net-new SLOC the Development prompt anchors to a screen / integration: (low, high) across the
# simple→complex tiers. Used only as a GROSS sanity band — the SLACK widens it 50% so normal
# variation never trips, and we flag only when the twin's realized SLOC lands well outside it.
_SLOC_PER_SCREEN_LO, _SLOC_PER_SCREEN_HI = 150.0, 700.0
_SLOC_PER_INTEGRATION_LO, _SLOC_PER_INTEGRATION_HI = 300.0, 800.0
_SLOC_SANITY_SLACK = 1.5


def _screen_count(state: EstimationState) -> int:
    """Stated screen count — stage2 first, then parsed signals (mirrors the code_review twin)."""
    stage2 = state.get("stage2")
    if stage2 is not None and stage2.screen_count_estimate:
        return stage2.screen_count_estimate
    parsed = state.get("parsed_context") or {}
    return int(parsed.get("screen_count_estimate") or 0) if isinstance(parsed, dict) else 0


def _integration_count(state: EstimationState) -> int:
    """Stated integration count — stage2 (count or list), then parsed mentions."""
    stage2 = state.get("stage2")
    if stage2 is not None:
        if stage2.integration_count:
            return stage2.integration_count
        if stage2.integration_list:
            return len(stage2.integration_list)
    parsed = state.get("parsed_context") or {}
    return len(parsed.get("integration_mentions") or []) if isinstance(parsed, dict) else 0


def _dev_sloc_screen_consistency_warning(
    pass2: list[PhaseEstimate], state: EstimationState
) -> str | None:
    """Cross-check the Development twin's realized SLOC against an independent screen/integration
    estimate. The twins are independent, so development free-sizes SLOC while code_review sizes the
    same codebase from screen counts — this catches gross divergence (the classic boilerplate
    over-count, or a wild undersize). A sanity flag only; it does NOT change the estimate."""
    dev = next((pe for pe in pass2 if pe.phase == Phase.DEVELOPMENT), None)
    if dev is None or not dev.breakdown:
        return None
    ksloc = dev.breakdown.get("ksloc")
    screens = _screen_count(state)
    if not ksloc or screens <= 0:  # no realized SLOC, or no screen signal to check against
        return None
    sloc = ksloc * 1000.0
    integ = _integration_count(state)
    lo = (screens * _SLOC_PER_SCREEN_LO + integ * _SLOC_PER_INTEGRATION_LO) / _SLOC_SANITY_SLACK
    hi = (screens * _SLOC_PER_SCREEN_HI + integ * _SLOC_PER_INTEGRATION_HI) * _SLOC_SANITY_SLACK
    per_screen = sloc / screens
    if sloc > hi:
        return (
            f"Development sized {sloc:,.0f} SLOC (~{per_screen:,.0f}/screen) for {screens} screens "
            f"+ {integ} integrations — well above the ~700 net-new SLOC/screen ceiling; likely "
            f"counting framework/boilerplate."
        )
    if sloc < lo:
        return (
            f"Development sized only {sloc:,.0f} SLOC (~{per_screen:,.0f}/screen) for {screens} "
            f"screens + {integ} integrations — unusually low; the build may be undersized."
        )
    return None


def _capers_jones_qa_ratio_warning(pass2: list[PhaseEstimate]) -> str | None:
    """QA effort should typically be 30-40% of total. Flag if way outside that band.

    The ratio is only meaningful when the build phase that anchors the denominator (development)
    is in scope alongside QA. Requiring both excludes deliberately-partial scopes where the band
    doesn't apply and the warning would mislead — e.g. a dev-less subset (discovery+ux+qa) where
    QA trivially dominates, or any scope without QA where it reads as 0%. When both are present
    (including a focused development+QA scope) the share is a real signal, so we still flag it."""
    by_phase = {pe.phase: pe.ai_assisted_hours.pert_mean for pe in pass2}
    if Phase.QA_TESTING not in by_phase or Phase.DEVELOPMENT not in by_phase:
        return None
    qa = by_phase[Phase.QA_TESTING]
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

    if (w := _dev_sloc_screen_consistency_warning(pass2, state)) is not None:
        warnings.append(w)

    logger.info(
        "consistency_check complete: %d phase(s), %d warning(s)", len(pass2), len(warnings)
    )
    return {"consistency_warnings": warnings}

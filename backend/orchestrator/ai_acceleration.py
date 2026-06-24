"""AI-acceleration model: how much AI realistically reduces — or sometimes
*increases* — the effort for a given SDLC phase.

The per-(phase × tooling) reduction is a **guardrail range** `[lo, hi]`, not a fixed
multiplier. The twin LLM proposes a reduction for the phase and it is clamped into
that band ("the LLM proposes within guardrails"); phases with no LLM proposal
(Discovery/UX) use the band midpoint. The band is then moderated by codebase
context and team seniority, with a small penalty for regulated / large-familiar
brownfield work that can push the result **negative** (AI net-slower — METR 2025).

    band      = reduction_bands[phase][tooling]        # [lo, hi]
    base      = clamp(llm_proposed, lo, hi)             # LLM within guardrails
    moderated = base × codebase_factor × seniority_factor
    effective = clamp(moderated − penalty, NEGATIVE_FLOOR, hi)

Bands are tunable in the database (`ai_reduction_bands` table) and loaded into the
graph state at parse time; ``DEFAULT_BANDS`` below is the in-code fallback so the
estimator still runs when Postgres is unavailable. The DB overrides per cell.
All other constants live here and are tunable; the six twins call
``effective_ai_reduction``.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.project_schema import AiToolingLevel, CodebaseContext, RoleRoster
from models.twin_outputs import Phase, RoleSeniority

# Default per-(phase, tooling) reduction guardrail bands (lo, hi) as fractions, in
# NEUTRAL conditions (greenfield, balanced team). Deliberately conservative — these
# are realized, team-level NET reductions, not isolated-task speedups. The DB
# `ai_reduction_bands` table overrides any cell; this is the fallback.
DEFAULT_BANDS: dict[tuple[Phase, AiToolingLevel], tuple[float, float]] = {
    # Autocomplete (inline tab-completion) is a code-writing assist, so it does NOT
    # apply to discovery, UX design, or code review — those phases have no
    # AUTOCOMPLETE band (band_for falls back to (0, 0) → zero reduction, no overhead).
    (Phase.DISCOVERY, AiToolingLevel.NONE): (0.00, 0.00),
    (Phase.DISCOVERY, AiToolingLevel.CHAT): (0.06, 0.18),
    (Phase.DISCOVERY, AiToolingLevel.AGENTIC): (0.12, 0.24),
    (Phase.UX_DESIGN, AiToolingLevel.NONE): (0.00, 0.00),
    (Phase.UX_DESIGN, AiToolingLevel.CHAT): (0.04, 0.14),
    (Phase.UX_DESIGN, AiToolingLevel.AGENTIC): (0.08, 0.20),
    (Phase.DEVELOPMENT, AiToolingLevel.NONE): (0.00, 0.00),
    (Phase.DEVELOPMENT, AiToolingLevel.AUTOCOMPLETE): (0.08, 0.18),
    (Phase.DEVELOPMENT, AiToolingLevel.CHAT): (0.24, 0.48),
    (Phase.DEVELOPMENT, AiToolingLevel.AGENTIC): (0.45, 0.72),
    (Phase.CODE_REVIEW, AiToolingLevel.NONE): (0.00, 0.00),
    (Phase.CODE_REVIEW, AiToolingLevel.CHAT): (0.10, 0.22),
    (Phase.CODE_REVIEW, AiToolingLevel.AGENTIC): (0.16, 0.30),
    (Phase.DEPLOYMENT, AiToolingLevel.NONE): (0.00, 0.00),
    (Phase.DEPLOYMENT, AiToolingLevel.AUTOCOMPLETE): (0.00, 0.08),
    (Phase.DEPLOYMENT, AiToolingLevel.CHAT): (0.06, 0.16),
    (Phase.DEPLOYMENT, AiToolingLevel.AGENTIC): (0.10, 0.22),
    (Phase.QA_TESTING, AiToolingLevel.NONE): (0.00, 0.00),
    (Phase.QA_TESTING, AiToolingLevel.AUTOCOMPLETE): (0.06, 0.14),
    (Phase.QA_TESTING, AiToolingLevel.CHAT): (0.12, 0.24),
    (Phase.QA_TESTING, AiToolingLevel.AGENTIC): (0.20, 0.34),
}

# AI can make work net-slower; this is the floor of effective_ai_reduction.
NEGATIVE_FLOOR = -0.15

# Codebase context moderates how much of the band is realized.
CODEBASE_FACTOR: dict[CodebaseContext, float] = {
    CodebaseContext.GREENFIELD: 1.00,
    CodebaseContext.BROWNFIELD_SMALL: 0.85,
    CodebaseContext.BROWNFIELD_LARGE_UNFAMILIAR: 0.70,
    CodebaseContext.BROWNFIELD_LARGE_FAMILIAR: 0.40,
}

# Subtractive penalties that can flip the result negative (verification overhead).
_REGULATED_PENALTY = 0.08
_FAMILIAR_BROWNFIELD_PENALTY = 0.06


def default_bands() -> list[tuple[Phase, AiToolingLevel, float, float]]:
    """All editable default ``(phase, tooling, lo, hi)`` bands for the admin UI.

    Excludes the NONE tooling level — "no tooling" always means zero reduction, so it
    is not configurable (and an override on it would wrongly grant a reduction).
    Ordered by phase then tooling for stable rendering.
    """
    return [
        (phase, tooling, lo, hi)
        for (phase, tooling), (lo, hi) in DEFAULT_BANDS.items()
        if tooling is not AiToolingLevel.NONE
    ]


def band_for(
    phase: Phase, tooling: AiToolingLevel, overrides: dict | None = None
) -> tuple[float, float]:
    """Return the (lo, hi) reduction guardrail band for a (phase, tooling) pair.

    `overrides` is the DB-loaded, nested ``{phase_value: {tooling_value: [lo, hi]}}``
    dict carried in graph state; any present cell wins over ``DEFAULT_BANDS``.
    """
    if overrides:
        cell = overrides.get(phase.value, {}).get(tooling.value)
        if cell and len(cell) == 2:
            return float(cell[0]), float(cell[1])
    return DEFAULT_BANDS.get((phase, tooling), (0.0, 0.0))


def seniority_factor(roster: RoleRoster | None) -> float:
    """Effort-share-weighted seniority moderator. Juniors capture more AI gain;
    seniors (especially on familiar code) capture less. Uses a **softened ±0.2**
    effort-share swing (was ±0.3), so a senior-heavy team is moderated less harshly.
    Clamped to [0.6, 1.25]."""
    if roster is None or not roster.roles:
        return 1.0
    total = sum(r.percentage for r in roster.roles) or 100.0
    senior = sum(r.percentage for r in roster.roles if r.seniority == RoleSeniority.SENIOR)
    junior = sum(r.percentage for r in roster.roles if r.seniority == RoleSeniority.JUNIOR)
    factor = 1.0 + 0.2 * (junior / total) - 0.2 * (senior / total)
    return max(0.6, min(1.25, factor))


@dataclass(frozen=True)
class ReductionContext:
    """The constant (per-phase) inputs to ``effective_ai_reduction`` — everything except the
    per-draw ``proposed_reduction``. Built once per phase and threaded through the Monte Carlo
    reduction sampler so the moderation/clamp re-runs on every draw without re-deriving these.
    Replaces the untyped dict that was duplicated in ``_twin_base`` and the WBS rollup."""

    phase: Phase
    codebase: CodebaseContext
    tooling: AiToolingLevel
    roster: RoleRoster | None
    regulated: bool
    bands: dict | None

    def reduction_kwargs(self) -> dict:
        """The keyword args to splat into ``effective_ai_reduction`` (all but ``proposed_reduction``)."""
        return {
            "phase": self.phase,
            "codebase": self.codebase,
            "tooling": self.tooling,
            "roster": self.roster,
            "regulated": self.regulated,
            "bands": self.bands,
        }


def effective_ai_reduction(
    *,
    phase: Phase,
    tooling: AiToolingLevel,
    codebase: CodebaseContext,
    roster: RoleRoster | None,
    proposed_reduction: float | None = None,
    regulated: bool = False,
    bands: dict | None = None,
) -> float:
    """Realized AI effort reduction for a phase, in ``[NEGATIVE_FLOOR, band_hi]``.

    `proposed_reduction` is the twin's LLM-proposed reduction (0..1); it is clamped
    into the (phase, tooling) guardrail band. Pass None (Discovery/UX, which have no
    LLM proposal) to use the band midpoint. May be negative.
    """
    lo, hi = band_for(phase, tooling, bands)
    if hi <= 0.0:
        return 0.0  # no tooling for this phase → no change, no overhead
    base = (lo + hi) / 2.0 if proposed_reduction is None else min(max(proposed_reduction, lo), hi)
    moderated = base * CODEBASE_FACTOR[codebase] * seniority_factor(roster)
    penalty = (_REGULATED_PENALTY if regulated else 0.0) + (
        _FAMILIAR_BROWNFIELD_PENALTY
        if codebase == CodebaseContext.BROWNFIELD_LARGE_FAMILIAR
        else 0.0
    )
    return max(NEGATIVE_FLOOR, min(moderated - penalty, hi))

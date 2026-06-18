"""Team-scaling model for staffing: Brooks's Law (coordination overhead) and the law of
diminishing returns (sublinear throughput).

`synthesize_estimate` calls this (project-level, post-fan-out) to inflate cost + schedule by
Brooks's coordination overhead and to recommend an optimal team size + a scaling-efficiency
readout from the combined throughput curve. ``n`` is the total team size (Σ headcount).

Two distinct, separately-tunable effects:
  - **Brooks coordination overhead** ``o(n)`` — capacity lost to the n(n−1)/2 communication
    links; grows with team size, used to inflate cost + schedule.
  - **Diminishing returns** ``n**β`` (β<1) — imperfect partitionability (Amdahl): n people
    deliver < n× output. Shapes the throughput/duration curve and the optimal team size, but
    NOT cost (the per-algorithm effort estimates already embed a normal team's productivity —
    COCOMO's scale exponent is itself a diseconomy term — so a second penalty would double-count).

Coefficients are DB-tunable (``db.repositories.staffing.get_staffing_coefficients``), falling
back to ``DEFAULT_STAFFING_COEFFS``. Pure stdlib. Defaults are deliberately gentle: these model
the *marginal* over/under-staffing effect, not a solo→team penalty.
"""

from __future__ import annotations

import math

# Code defaults; the DB ``staffing_coefficients`` table overrides any key.
DEFAULT_STAFFING_COEFFS: dict[str, float] = {
    "link_cost": 0.06,                     # capacity fraction lost per communication link
    "free_team_size": 3.0,                 # no coordination overhead at/below this size
    "overhead_cap": 0.40,                  # max coordination overhead applied to cost/schedule
    "diminishing_returns_exponent": 0.90,  # β in n**β throughput (1.0 = perfectly parallel)
}

# Editable range per coefficient (the Settings admin validates against these).
STAFFING_COEFF_BOUNDS: dict[str, tuple[float, float]] = {
    "link_cost": (0.0, 0.5),
    "free_team_size": (0.0, 50.0),
    "overhead_cap": (0.0, 1.0),
    "diminishing_returns_exponent": (0.5, 1.0),
}

# When recommending a team size, give each person at least this many weeks of work, so the
# recommendation SCALES with project size (small project → small team) instead of pinning to the
# coordination throughput peak. Larger = leaner teams / longer schedules. Tunable.
_MIN_WEEKS_PER_PERSON = 16.0


def _coeff(coeffs: dict[str, float] | None, key: str) -> float:
    if coeffs and key in coeffs:
        return coeffs[key]
    return DEFAULT_STAFFING_COEFFS[key]


def _raw_overhead(team_size: int, coeffs: dict[str, float] | None) -> float:
    """Unbounded Brooks coordination loss: ``link_cost·(n−1)/2`` above the free team size, else
    0. The throughput curve uses this (so a real optimum exists, where coordination eventually
    overtakes the n**β gain); the *cost* multiplier uses the capped ``coordination_overhead``."""
    n = max(0, team_size)
    if n <= _coeff(coeffs, "free_team_size"):
        return 0.0
    return _coeff(coeffs, "link_cost") * (n - 1) / 2.0


def coordination_overhead(team_size: int, coeffs: dict[str, float] | None = None) -> float:
    """Brooks's Law coordination overhead ``o(n)`` used to inflate cost + schedule — the raw
    communication loss clamped to ``overhead_cap`` so a large team can't explode the cost.
    Returns a fraction in ``[0, overhead_cap]``."""
    return max(0.0, min(_coeff(coeffs, "overhead_cap"), _raw_overhead(team_size, coeffs)))


def team_throughput(team_size: int, coeffs: dict[str, float] | None = None) -> float:
    """Effective throughput in person-equivalents, combining both laws:
    ``n**β · max(0, 1 − raw_overhead(n))``. Equals ``n`` only when β=1 and ``n ≤ free_team_size``.
    Rises then falls as the (unbounded) coordination loss overtakes the n**β gain — its argmax is
    the optimal team size."""
    n = max(0, team_size)
    if n <= 0:
        return 0.0
    beta = _coeff(coeffs, "diminishing_returns_exponent")
    return (n**beta) * max(0.0, 1.0 - _raw_overhead(n, coeffs))


def staffing_efficiency(team_size: int, coeffs: dict[str, float] | None = None) -> float:
    """Realized fraction of ideal *linear* scaling: ``team_throughput(n)/n``. 1.0 for a solo dev,
    decreasing as the team grows (diminishing returns + coordination). The 'scaling efficiency %'
    readout."""
    n = max(0, team_size)
    if n <= 0:
        return 0.0
    return team_throughput(n, coeffs) / n


def optimal_team_size(
    effort_hours: float,
    hours_per_week: float,
    coeffs: dict[str, float] | None = None,
    *,
    max_team: int = 50,
) -> int:
    """Recommended team size. It SCALES with project size: large enough that each person carries
    ≥ ``_MIN_WEEKS_PER_PERSON`` of work (so a small project gets a small team), capped at the
    coordination throughput peak (the argmax of ``team_throughput`` — beyond it more people only
    *lengthen* the schedule). So a tiny project → 1–2, a mid MVP → a handful, a large program →
    the peak; it is NOT a fixed number pinned by the coefficients alone."""
    if effort_hours <= 0 or hours_per_week <= 0:
        return 1
    work_bound = max(1, math.ceil(effort_hours / (hours_per_week * _MIN_WEEKS_PER_PERSON)))
    upper = max(1, min(max_team, work_bound))
    best_n, best_tp = 1, team_throughput(1, coeffs)
    for n in range(2, upper + 1):
        tp = team_throughput(n, coeffs)
        if tp > best_tp:
            best_tp, best_n = tp, n
    return best_n

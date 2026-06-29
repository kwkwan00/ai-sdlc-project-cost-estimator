"""Monte Carlo uncertainty propagation for the estimation twins.

Each twin's LLM proposes POINT algorithm inputs; the deterministic ``compute_*``
turns them into a scalar ``manual_mid``. Historically that mid was wrapped in a
fixed ±factor PERT band (``pert_range``) regardless of how (un)certain the inputs
were. This module replaces that with a Monte Carlo layer that propagates THREE
uncertainty sources through the **unchanged** ``compute_*`` functions:

    base_i   = compute_*(sampled size drivers)          # input-size uncertainty (nonlinear)
    r_i      = reduction_sampler(rng)                    # AI-effectiveness uncertainty
    risk_i   = sum_k Bernoulli(p_k) * PERT(low_k, high_k)# discrete risk events
    manual_i = base_i + risk_i
    ai_i     = base_i * (1 - r_i) + risk_i               # risks hit both scenarios undiscounted

The *modal* draw is "no risk fires + point reduction", so the deterministic
``most_likely`` and the ``ai.most_likely == manual.most_likely * (1 - r_point)``
identity survive — only the band widens. Pure stdlib (``random`` + ``statistics``
+ ``math``); no numpy/scipy. Seeded per (estimate, phase, pass) for reproducible,
phase-independent streams that are safe under the parallel twin fan-out.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.twin_outputs import HourRange


# Default Monte Carlo draws. Env-overridable so tests can drop it (e.g. 200) for
# speed/determinism. At 2000 the MC-mean standard error on a CoV≈0.3 driver is
# ~0.7% of the mean — under the eval harness's 2% tolerance.
def _draws_from_env(default: int = 2000) -> int:
    """Parse MC_DRAWS, degrading to the default on a malformed/non-positive value.

    Evaluated at import time, so a bad env value must NOT raise — that would crash the whole
    orchestrator module import rather than harmlessly falling back.
    """
    raw = os.getenv("MC_DRAWS")
    if raw is None:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return n if n > 0 else default


DEFAULT_DRAWS = _draws_from_env()


# Systemic ("common-factor") uncertainty shared across WBS leaves. Summing leaves as INDEPENDENT
# makes the combined band collapse ~1/√N as the tree grows — backwards for real projects, where a
# hard team/domain makes MANY tasks overrun together. Each draw multiplies the leaf sum by ONE shared
# lognormal factor exp(N(0, σ)): median 1 (so the deterministic mode / most_likely is untouched) and
# right-skewed (overruns heavier than equivalent underruns, matching the reduction sampler's
# deliberate pessimism). σ acts as a CoV floor on the combined band regardless of leaf count.
# Env-overridable; σ=0 recovers the exact independent-sum behavior.
def _leaf_corr_sigma_from_env(default: float = 0.15) -> float:
    """Parse MC_LEAF_CORR_SIGMA, degrading to the default on a malformed/negative value.

    Evaluated at import time, so a bad env value must NOT raise (it would crash the orchestrator
    module import); it harmlessly falls back instead.
    """
    raw = os.getenv("MC_LEAF_CORR_SIGMA")
    if raw is None:
        return default
    try:
        s = float(raw)
    except ValueError:
        return default
    # Require a finite, non-negative value: `inf`/`1e400` pass `>= 0` but blow every draw up to
    # inf → NaN hours/costs across the whole WBS estimate; `nan` already fails `>= 0`.
    return s if (math.isfinite(s) and s >= 0) else default


DEFAULT_LEAF_CORR_SIGMA = _leaf_corr_sigma_from_env()

_EPS = 1e-9
# Percentiles reported for the fan chart (HourRange.percentiles).
_PCTS: tuple[float, ...] = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)


class Range3(BaseModel):
    """A two-point uncertainty interval the LLM provides for a driver input.

    The *mode* is the existing point field on the ``*Inputs`` model; this carries
    only the ~80%-confidence ``low``/``high`` bounds. Coerces ``low <= high``
    instead of raising (mirrors ``HourRange``'s non-raising coercion).
    """

    model_config = ConfigDict(extra="forbid")
    low: float = Field(ge=0, description="Low end of the ~80% confidence interval")
    high: float = Field(ge=0, description="High end of the ~80% confidence interval")

    @model_validator(mode="after")
    def _order(self) -> Range3:
        if self.low > self.high:
            self.low, self.high = self.high, self.low
        return self


@dataclass(frozen=True)
class MCResult:
    """Summary of one scenario's Monte Carlo sample for a single phase."""

    point: float  # deterministic anchor (compute_*(point_inputs), pre/post reduction)
    p10: float
    p50: float
    p90: float
    mean: float
    std: float
    n: int
    degenerate: bool  # True when no uncertainty was supplied (all draws == point)
    percentiles: dict[str, float]


# A reduction sampler yields a realized AI reduction r in [-floor, hi] per draw.
ReductionSampler = Callable[[random.Random], float]


def make_rng(seed_material: str) -> random.Random:
    """Deterministic, phase-independent RNG. Seed material is
    ``f"{estimate_id}:{phase}:{pass_num}"`` so identical inputs reproduce and each
    phase gets an independent stream (variance-combine + parallel-fan-out safe)."""
    digest = hashlib.blake2b(seed_material.encode("utf-8"), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def sample_pert(low: float, mode: float, high: float, rng: random.Random, *, lam: float = 4.0) -> float:
    """Draw from a modified Beta-PERT(low, mode, high) with shape ``lam`` (4 = classic
    PERT). Uses stdlib ``rng.betavariate``; returns ``mode`` when the band is degenerate."""
    span = high - low
    if span <= _EPS:
        return mode
    m = min(max(mode, low), high)  # clamp the mode into [low, high]
    alpha = 1.0 + lam * (m - low) / span
    beta = 1.0 + lam * (high - m) / span
    return low + rng.betavariate(alpha, beta) * span


def sample_risks(risk_specs: Sequence[tuple[float, float, float]], rng: random.Random) -> float:
    """One draw's total incremental risk hours: each ``(probability, low, high)`` risk
    fires with its probability and, when it fires, adds PERT(low, midpoint, high) hours."""
    total = 0.0
    for prob, low, high in risk_specs:
        if prob > 0 and rng.random() < prob:
            total += sample_pert(low, (low + high) / 2.0, high, rng)
    return total


def _percentile(sorted_xs: list[float], q: float) -> float:
    """Type-7 (linear interpolation) percentile on a pre-sorted list. ``q`` in [0,1]."""
    n = len(sorted_xs)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_xs[0]
    rank = q * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_xs[lo]
    return sorted_xs[lo] + (rank - lo) * (sorted_xs[hi] - sorted_xs[lo])


def _summarize(draws: list[float], *, point: float) -> MCResult:
    draws_sorted = sorted(draws)
    pct = {f"p{int(q * 100)}": _percentile(draws_sorted, q) for q in _PCTS}
    std = statistics.pstdev(draws) if len(draws) > 1 else 0.0
    return MCResult(
        point=point,
        p10=pct["p10"],
        p50=pct["p50"],
        p90=pct["p90"],
        mean=statistics.fmean(draws) if draws else point,
        std=std,
        n=len(draws),
        degenerate=std <= _EPS,
        percentiles=pct,
    )


def propagate_phase(
    point_inputs: BaseModel,
    compute_fn: Callable[[Any], tuple[float, dict]],
    *,
    size_fields: dict[str, tuple[float, float, float]],
    reduction_sampler: ReductionSampler,
    risk_specs: Sequence[tuple[float, float, float]],
    eff_point: float,
    n_draws: int | None = None,
    rng: random.Random,
) -> tuple[MCResult, MCResult]:
    """Run the Monte Carlo for one phase and return ``(manual_result, ai_result)``.

    ``size_fields`` maps an input field name to its ``(low, mode, high)`` band;
    each draw perturbs those fields (via ``model_copy``) and re-runs the UNCHANGED
    ``compute_fn``. ``reduction_sampler`` yields a realized reduction per draw;
    ``risk_specs`` are ``(probability, low, high)`` tuples. The ``.point`` anchors
    are the deterministic mids (``compute_fn(point_inputs)`` and that × (1−eff_point)).

    ``n_draws`` defaults to ``MC_DRAWS`` resolved at CALL time (not import) so a test/env override
    actually takes effect even after this module is imported.
    """
    n_draws = n_draws if n_draws is not None else _draws_from_env()
    base_point = compute_fn(point_inputs)[0]
    manual_draws: list[float] = []
    ai_draws: list[float] = []
    for _ in range(n_draws):
        if size_fields:
            update = {f: sample_pert(lo, mode, hi, rng) for f, (lo, mode, hi) in size_fields.items()}
            base = compute_fn(point_inputs.model_copy(update=update))[0]
        else:
            base = base_point
        r = reduction_sampler(rng)
        risk = sample_risks(risk_specs, rng)
        manual_draws.append(base + risk)
        ai_draws.append(base * (1.0 - r) + risk)
    manual = _summarize(manual_draws, point=base_point)
    ai = _summarize(ai_draws, point=base_point * (1.0 - eff_point))
    return manual, ai


def combine_pert_leaves(
    leaves: Sequence[tuple[float, float, float]],
    *,
    reduction_sampler: ReductionSampler,
    eff_point: float,
    rng: random.Random,
    n_draws: int | None = None,
    leaf_corr_sigma: float | None = None,
) -> tuple[MCResult, MCResult]:
    """Bottom-up sibling of ``propagate_phase`` for the WBS flow.

    Each ``leaves`` entry is a leaf's ``(low, mode, high)`` PERT band. Every draw sums one
    Beta-PERT sample per leaf, then multiplies that sum by ONE shared lognormal ``factor =
    exp(N(0, leaf_corr_sigma))`` before applying a per-draw AI reduction ``ai_i = manual_i ·
    (1 − r)``. The shared factor is a **systemic ("common-factor") correlation**: the leaves are no
    longer purely independent (which collapses the band ~1/√N as the tree grows) — a single project
    can run hard or easy across ALL its tasks at once. The factor's median is 1, so it widens the band
    without moving the deterministic anchors: ``point = Σ mode`` for manual and ``point · (1 −
    eff_point)`` for AI — ``most_likely`` is exactly Σ leaf modes and the ``ai.most_likely ==
    manual.most_likely · (1 − eff_point)`` identity holds. ``leaf_corr_sigma=0`` recovers the exact
    independent-sum behavior. Returns ``(manual_result, ai_result)`` via ``_summarize`` so
    ``result_to_hour_range`` consumes them unchanged. Pure stdlib; no risk events at the leaf level
    (the three-point band already carries the leaf's uncertainty). ``n_draws`` (``MC_DRAWS``) and
    ``leaf_corr_sigma`` (``MC_LEAF_CORR_SIGMA``) both default to their env values resolved at CALL time
    (not import) so a test/env override takes effect after import."""
    n_draws = n_draws if n_draws is not None else _draws_from_env()
    leaf_corr_sigma = leaf_corr_sigma if leaf_corr_sigma is not None else _leaf_corr_sigma_from_env()
    point = sum(mode for (_lo, mode, _hi) in leaves)
    if not leaves:
        zero = _summarize([0.0], point=0.0)
        return zero, zero
    manual_draws: list[float] = []
    ai_draws: list[float] = []
    for _ in range(n_draws):
        # One shared factor per draw, applied to every leaf → positive cross-leaf correlation.
        factor = math.exp(rng.gauss(0.0, leaf_corr_sigma)) if leaf_corr_sigma > 0 else 1.0
        base = factor * sum(sample_pert(lo, mode, hi, rng) for (lo, mode, hi) in leaves)
        r = reduction_sampler(rng)
        manual_draws.append(base)
        ai_draws.append(base * (1.0 - r))
    manual = _summarize(manual_draws, point=point)
    ai = _summarize(ai_draws, point=point * (1.0 - eff_point))
    return manual, ai


def result_to_hour_range(mc: MCResult) -> HourRange:
    """Map an ``MCResult`` onto a three-point ``HourRange`` (drop-in for ``pert_range``):
    most_likely is ALWAYS the deterministic mode (``point``); optimistic/pessimistic are
    P10/P90 *expanded to bracket the point* so the ordering holds without ever moving the
    mode. Keeping most_likely == point is load-bearing: role-hours are attributed off the
    same deterministic mid, so ``sum(role_hours) == most_likely`` exactly (the
    role-attribution invariant) and ``estimate_accuracy`` reads a stable mid. When a
    high-probability risk shifts the whole band up, the mode can sit below P10 — then
    optimistic collapses to the point (best case = no risk fires = base), which is honest."""
    point = max(0.0, mc.point)
    return HourRange(
        optimistic=max(0.0, min(mc.p10, point)),
        most_likely=point,
        pessimistic=max(point, mc.p90),
        std=max(0.0, mc.std),
        mean=max(0.0, mc.mean),
        percentiles={k: max(0.0, v) for k, v in mc.percentiles.items()},
    )


def resolve_size_band(
    *,
    point_value: float,
    explicit: Range3 | None,
    estimate_cov: float | None,
    confidence: float,
    lo_bound: float | None = None,
    hi_bound: float | None = None,
) -> tuple[float, float, float] | None:
    """Resolve a driver's ``(low, mode, high)`` band via the fallback ladder:
    explicit ``Range3`` → ``estimate_cov`` → confidence-derived CoV. Returns None
    only when ``point_value`` is non-positive (nothing to perturb). The band is
    clamped to ``[lo_bound, hi_bound]`` when given (so ``compute_*`` never runs on
    out-of-range inputs, e.g. a continuous factor with ``Field(ge=, le=)`` bounds)."""
    if point_value <= 0:
        return None
    if explicit is not None:
        low, high = explicit.low, explicit.high
    else:
        cov = estimate_cov if estimate_cov is not None else _confidence_cov(confidence)
        low = point_value * (1.0 - 1.5 * cov)
        high = point_value * (1.0 + 2.0 * cov)
    low = max(0.0, min(low, point_value))
    high = max(high, point_value)
    if lo_bound is not None:
        low = max(low, lo_bound)
    if hi_bound is not None:
        high = min(high, hi_bound)
    mode = min(max(point_value, low), high)
    return (low, mode, high)


def _confidence_cov(confidence: float) -> float:
    """Confidence → coefficient of variation when the LLM gives no explicit band.
    High confidence → tight band; low confidence → wide. Clamped to [0.10, 0.45]."""
    return min(0.45, max(0.10, 0.12 + 0.35 * (1.0 - confidence)))

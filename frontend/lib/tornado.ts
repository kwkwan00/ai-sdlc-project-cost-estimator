/** Pure ranking logic for the sensitivity / tornado chart on the review page.
 *
 *  "Which phases drive the project's total uncertainty?" Each phase contributes a
 *  spread to the total; we rank phases by that spread, biggest first, so the chart
 *  reads top-to-bottom like a tornado. The recharts/Tailwind rendering lives in
 *  `components/TornadoChart.tsx`; this module is the unit-testable math.
 *
 *  Spread metric, per phase (for the toggle-selected scenario's `HourRange`):
 *   - When the range was simulated and carries a finite `std`, we use the 80%
 *     interval width derived from the normal approximation (P10..P90 ≈ 2·1.2816·σ)
 *     so the bar matches the fan chart's dark band. If P10/P90 percentiles are
 *     present we use their exact width instead (no distributional assumption).
 *   - Otherwise we fall back to the deterministic band width
 *     `pessimistic − optimistic`.
 *  The chosen metric is reported as `spread`; `low`/`high` bound the bar so the
 *  component can draw a floating horizontal range centered near the most-likely. */

import { hasPercentiles } from "./fan-chart";
import { PHASE_LABELS, type Phase, type PhaseEstimate } from "./types";

/** z-score for an 80% central interval (P10..P90): Φ⁻¹(0.9) ≈ 1.2816. Used to turn
 *  a std-dev into a band width when explicit percentiles are absent. */
export const Z80 = 1.2815515594465999;

export interface TornadoRow {
  phase: Phase;
  /** Human-readable phase name (from `PHASE_LABELS`). */
  label: string;
  /** Uncertainty magnitude used for ranking (hours). Always ≥ 0. */
  spread: number;
  /** Lower bound of the drawn bar (hours). */
  low: number;
  /** Upper bound of the drawn bar (hours). */
  high: number;
  /** Most-likely value the bar is centered on (hours). */
  mid: number;
  /** This phase's spread as a fraction (0..1) of the summed spread across phases. */
  share: number;
  /** True when `spread`/bounds came from simulated percentiles or std, false when
   *  derived from the deterministic optimistic/pessimistic fallback. */
  simulated: boolean;
}

/** The `HourRange` to rank for a phase, picked by the active scenario toggle. */
type Mode = "ai_assisted" | "manual_only";

/** Compute one phase's uncertainty band + spread for the given scenario.
 *
 *  Preference order for the band: explicit P10/P90 percentiles → std-derived 80%
 *  interval around the most-likely → deterministic optimistic/pessimistic. */
export function phaseSpread(phase: PhaseEstimate, mode: Mode): TornadoRow {
  const range = mode === "ai_assisted" ? phase.ai_assisted_hours : phase.manual_only_hours;
  const mid = range.most_likely;

  if (hasPercentiles(range)) {
    const low = range.percentiles.p10;
    const high = range.percentiles.p90;
    return {
      phase: phase.phase,
      label: PHASE_LABELS[phase.phase],
      spread: Math.max(0, high - low),
      low,
      high,
      mid,
      share: 0, // filled in by buildTornado once the total is known
      simulated: true,
    };
  }

  if (typeof range.std === "number" && Number.isFinite(range.std) && range.std > 0) {
    const half = Z80 * range.std;
    return {
      phase: phase.phase,
      label: PHASE_LABELS[phase.phase],
      spread: 2 * half,
      low: mid - half,
      high: mid + half,
      mid,
      share: 0,
      simulated: true,
    };
  }

  // Deterministic fallback — three-point band width.
  return {
    phase: phase.phase,
    label: PHASE_LABELS[phase.phase],
    spread: Math.max(0, range.pessimistic - range.optimistic),
    low: range.optimistic,
    high: range.pessimistic,
    mid,
    share: 0,
    simulated: false,
  };
}

/** Rank every phase by its uncertainty spread (largest first) for one scenario and
 *  attach each phase's `share` of the summed spread. Stable on ties (preserves the
 *  caller's phase order). Empty input → empty output. */
export function buildTornado(phases: PhaseEstimate[], mode: Mode): TornadoRow[] {
  const rows = phases.map((p) => phaseSpread(p, mode));
  const totalSpread = rows.reduce((s, r) => s + r.spread, 0);
  const withShare = rows.map((r) => ({
    ...r,
    share: totalSpread > 0 ? r.spread / totalSpread : 0,
  }));
  // Descending by spread; stable for equal spreads (index tiebreak keeps input order).
  return withShare
    .map((r, i) => ({ r, i }))
    .sort((a, b) => b.r.spread - a.r.spread || a.i - b.i)
    .map(({ r }) => r);
}

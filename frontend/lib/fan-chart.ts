/** Pure helpers for the Monte Carlo fan chart. Kept here (not in components) so the
 *  distribution math is unit-testable; the recharts/Tailwind rendering lives in
 *  `components/FanChart.tsx`.
 *
 *  The backend emits, on each `HourRange`, optional `percentiles`
 *  (`{p5,p10,p25,p50,p75,p90,p95}`) plus `optimistic` (=P10), `most_likely`
 *  (deterministic mid) and `pessimistic` (=P90). When percentiles are present we
 *  describe the distribution as nested confidence bands (P5–P95 outer, P10–P90
 *  inner). When they are absent — legacy or stub estimates — we degrade to the
 *  three-point optimistic/most_likely/pessimistic triangle so old estimates still
 *  render. */

import type { HourRange } from "./types";

/** Rounded hours with thousands separators but NO unit suffix — used to build
 *  "lo–hi h" ranges where the unit is shared (unlike `formatHours`, which suffixes
 *  every value and only separates ≥10k). */
function hoursNum(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

/** The ordered percentile keys the backend emits, low → high. */
export const PERCENTILE_KEYS = [
  "p5",
  "p10",
  "p25",
  "p50",
  "p75",
  "p90",
  "p95",
] as const;

export type PercentileKey = (typeof PERCENTILE_KEYS)[number];

/** One recharts row describing the distribution as horizontal confidence bands.
 *
 *  The chart is a single category ("Hours") laid out vertically, with hours on the
 *  value axis. `outer`/`inner` are `[low, high]` tuples consumed directly by ranged
 *  recharts `<Area>`s (recharts draws a band when a dataKey returns a `[min, max]`
 *  tuple). `mid` is the deterministic most-likely marker; `p50` is the simulated
 *  median when available. A single-row series keeps the helper trivially testable —
 *  assertions read the tuples directly. */
export interface FanPoint {
  /** Category-axis label for this row (the scenario name, e.g. "AI-assisted"). */
  label: string;
  /** Outer band: [P5, P95] when simulated, else [optimistic, pessimistic]. */
  outer: [number, number];
  /** Inner band: [P10, P90] when simulated, else [optimistic, pessimistic]. */
  inner: [number, number];
  /** Deterministic most-likely value — rendered as a reference marker/line. */
  mid: number;
  /** Simulated median (P50), when percentiles are present; else undefined. */
  p50?: number;
  /** Whether `outer`/`inner` came from real percentiles (true) or the
   *  three-point fallback (false). Lets the component label the bands honestly. */
  simulated: boolean;
}

/** True when `range.percentiles` carries the full p5..p95 ladder we render bands
 *  from. A partial/empty dict is treated as absent so we fall back cleanly. */
export function hasPercentiles(
  range: HourRange,
): range is HourRange & { percentiles: Record<PercentileKey, number> } {
  const p = range.percentiles;
  if (!p) return false;
  return PERCENTILE_KEYS.every((k) => typeof p[k] === "number" && Number.isFinite(p[k]));
}

/** Build the (single-row) recharts series for one `HourRange`.
 *
 *  With percentiles: outer = [P5, P95], inner = [P10, P90], p50 = median.
 *  Without: outer = inner = [optimistic, pessimistic] so a degenerate band still
 *  draws, and `mid` remains the most-likely marker. */
export function toFanSeries(range: HourRange, label = "Hours"): FanPoint[] {
  if (hasPercentiles(range)) {
    const p = range.percentiles;
    return [
      {
        label,
        outer: [p.p5, p.p95],
        inner: [p.p10, p.p90],
        mid: range.most_likely,
        p50: p.p50,
        simulated: true,
      },
    ];
  }
  // Fallback: no simulation data — collapse both bands to the deterministic
  // optimistic→pessimistic spread.
  return [
    {
      label,
      outer: [range.optimistic, range.pessimistic],
      inner: [range.optimistic, range.pessimistic],
      mid: range.most_likely,
      simulated: false,
    },
  ];
}

/** Human-readable 80% confidence interval drawn from P10–P90.
 *
 *  "80% confident: 8,400–12,900 h" when simulated. When percentiles are absent we
 *  still produce a sentence from the deterministic optimistic/pessimistic spread,
 *  labelled "estimated range" rather than a confidence percentage so we don't
 *  overstate certainty. */
export function confidenceLabel(range: HourRange): string {
  // Shared-unit "lo–hi h" form to match the spec example "8,400–12,900 h"
  // (one trailing unit, both bounds separated) — `formatHours` suffixes each value
  // and only separates ≥10k, so we format the bounds with `hoursNum` instead.
  if (hasPercentiles(range)) {
    const { p10, p90 } = range.percentiles;
    return `80% confident: ${hoursNum(p10)}–${hoursNum(p90)} h`;
  }
  return `Estimated range: ${hoursNum(range.optimistic)}–${hoursNum(range.pessimistic)} h`;
}

/** Estimate P(AI scenario finishes in fewer hours than the manual scenario).
 *
 *  Heuristic (cheap, no resampling): we compare the two distributions at the seven
 *  matched percentile points. Treating each percentile pair as an equally-weighted
 *  draw from each distribution, the fraction of points where `ai < manual` is an
 *  unbiased estimate of P(AI < manual) under the (rough) assumption that the
 *  percentile ladders move together. Ties (`ai === manual`) count as a half-win,
 *  the standard convention for the Mann–Whitney-style overlap statistic. Returns a
 *  0..1 probability, or `null` when either side lacks percentiles (we can't bound
 *  the spread from three points reliably enough to quote a probability). */
export function pAiSavesTime(ai: HourRange, manual: HourRange): number | null {
  if (!hasPercentiles(ai) || !hasPercentiles(manual)) return null;
  let wins = 0;
  for (const k of PERCENTILE_KEYS) {
    const a = ai.percentiles[k];
    const m = manual.percentiles[k];
    if (a < m) wins += 1;
    else if (a === m) wins += 0.5;
  }
  return wins / PERCENTILE_KEYS.length;
}

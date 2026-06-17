/** Pure helpers for the per-phase risk register on the review page.
 *
 *  Each `Risk` is a probability × impact-range item:
 *  `{ description, likelihood (0..1), impact_hours_low, impact_hours_high }`.
 *  We rank risks by *expected* impact — `likelihood × midpoint(low, high)` — which
 *  is the standard severity proxy (probability times the mean of the impact range).
 *  Rendering lives in `components/RiskRegister.tsx`; this module is the testable math. */

import type { Phase, PhaseEstimate, Risk } from "./types";

/** Midpoint of a risk's [low, high] impact-hours range. */
export function impactMidpoint(risk: Risk): number {
  return (risk.impact_hours_low + risk.impact_hours_high) / 2;
}

/** Expected impact in hours: `likelihood × midpoint(low, high)`.
 *
 *  This is a severity proxy, not a statement about the schedule — it's the mean
 *  impact you'd budget for the risk weighted by how likely it is to occur. */
export function expectedRiskHours(risk: Risk): number {
  return risk.likelihood * impactMidpoint(risk);
}

/** Sort risks by expected impact, highest first. Pure: returns a new array and
 *  leaves the input untouched. Stable on ties (preserves the input order). */
export function sortRisks(risks: Risk[]): Risk[] {
  return risks
    .map((risk, i) => ({ risk, i }))
    .sort((a, b) => expectedRiskHours(b.risk) - expectedRiskHours(a.risk) || a.i - b.i)
    .map(({ risk }) => risk);
}

/** A risk paired with the phase it came from — for a combined, cross-phase register. */
export interface PhaseRisk {
  phase: Phase;
  risk: Risk;
  /** `expectedRiskHours(risk)`, precomputed for display + sorting. */
  expectedHours: number;
}

/** Flatten every phase's risks into one list, tagged with the owning phase and
 *  sorted by expected impact (highest first). Stable on ties. */
export function collectPhaseRisks(phases: PhaseEstimate[]): PhaseRisk[] {
  const all: { item: PhaseRisk; i: number }[] = [];
  let i = 0;
  for (const phase of phases) {
    for (const risk of phase.risks) {
      all.push({
        item: { phase: phase.phase, risk, expectedHours: expectedRiskHours(risk) },
        i: i++,
      });
    }
  }
  return all
    .sort((a, b) => b.item.expectedHours - a.item.expectedHours || a.i - b.i)
    .map(({ item }) => item);
}

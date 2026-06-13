/** Small pure helpers for the review page's graphical breakdown. Kept here (not in
 *  components) so they're unit-testable; the color/Tailwind mapping lives in the
 *  components that render them. */

export type ConfidenceLevel = "low" | "medium" | "high";

/** Bucket a 0..1 confidence into a tone for the confidence meter. */
export function confidenceLevel(value: number): ConfidenceLevel {
  if (value < 0.5) return "low";
  if (value < 0.75) return "medium";
  return "high";
}

/** A value's share of a total, as a whole percentage (0 when total ≤ 0). */
export function sharePct(value: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((value / total) * 100);
}

export interface ReconciledTotals {
  aiHours: number;
  manualHours: number;
  savedHours: number;
  aiCost: number;
  manualCost: number;
  savedCost: number;
}

/** Round the AI / manual totals to whole numbers and DERIVE the savings from those
 *  rounded figures, so the displayed numbers always reconcile exactly:
 *  `aiHours + savedHours === manualHours` (and likewise for cost). This avoids the
 *  drift between most-likely totals and the PERT-mean saving, and ±1 rounding gaps. */
export function reconciledTotals(input: {
  aiHours: number;
  manualHours: number;
  aiCost: number;
  manualCost: number;
}): ReconciledTotals {
  const aiHours = Math.round(input.aiHours);
  const manualHours = Math.round(input.manualHours);
  const aiCost = Math.round(input.aiCost);
  const manualCost = Math.round(input.manualCost);
  return {
    aiHours,
    manualHours,
    savedHours: manualHours - aiHours,
    aiCost,
    manualCost,
    savedCost: manualCost - aiCost,
  };
}

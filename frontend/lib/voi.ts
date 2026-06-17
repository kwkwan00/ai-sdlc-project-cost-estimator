/** Value-of-information (VoI) ranking for the Stage-4 clarifying questions.
 *
 *  Each `ClarifyingQuestion` carries `impact_hours` — the Pass-1 estimate of how
 *  many hours of ambiguity that gap represents. We rank questions by that figure so
 *  the user answers the highest-information questions first, tightening the estimate
 *  fastest.
 *
 *  IMPORTANT — this is an impact-based *proxy* for value of information, NOT true
 *  EVPI (expected value of perfect information). We deliberately keep it simple and
 *  honest: a bigger `impact_hours` means answering the question can move the
 *  estimate more, so it's worth more to answer. The copy in the UI says
 *  "could shift the estimate by ~Nh", never "EVPI". Rendering lives in the page;
 *  this module is the testable ordering + label math. */

import type { ClarifyingQuestion } from "./types";

/** The VoI signal for a question = its Pass-1 `impact_hours`, floored at 0 (a
 *  negative/NaN impact is treated as "no signal"). Centralized so the page and the
 *  label helper agree on the number. */
export function questionImpact(q: ClarifyingQuestion): number {
  const h = q.impact_hours;
  return typeof h === "number" && Number.isFinite(h) && h > 0 ? h : 0;
}

/** Sum of every question's impact — the denominator for relative VoI badges. */
export function totalImpact(questions: ClarifyingQuestion[]): number {
  return questions.reduce((s, q) => s + questionImpact(q), 0);
}

/** Rank questions by impact (highest information value first). Pure: returns a new
 *  array, input untouched. Stable on ties (preserves the backend's order). */
export function rankQuestions(questions: ClarifyingQuestion[]): ClarifyingQuestion[] {
  return questions
    .map((q, i) => ({ q, i }))
    .sort((a, b) => questionImpact(b.q) - questionImpact(a.q) || a.i - b.i)
    .map(({ q }) => q);
}

export interface VoiBadge {
  /** Whole-hour impact magnitude (0 when the question carries no usable signal). */
  hours: number;
  /** Short headline, e.g. "≈ 120h at stake" or "impact unknown". */
  text: string;
  /** Relative share of total impact as a whole percent (0..100), when a positive
   *  `total` is supplied; otherwise undefined. */
  sharePct?: number;
  /** Coarse tier for badge styling. "none" when there's no usable signal. */
  level: "high" | "medium" | "low" | "none";
}

/** Build a short, honest VoI badge for a question.
 *
 *  `text` reads "≈ Nh at stake" (impact magnitude). When `total` (the summed impact
 *  across all questions) is provided and positive, `sharePct` is this question's
 *  relative slice, and the tier is derived from that share (≥40% high, ≥15% medium,
 *  else low). Without a total we tier on absolute hours (≥80h high, ≥20h medium).
 *  Questions with no usable impact get `level: "none"` and "impact unknown". */
export function voiLabel(q: ClarifyingQuestion, total?: number): VoiBadge {
  const hours = Math.round(questionImpact(q));
  if (hours <= 0) {
    return { hours: 0, text: "impact unknown", level: "none" };
  }

  if (typeof total === "number" && total > 0) {
    const sharePct = Math.round((questionImpact(q) / total) * 100);
    const level = sharePct >= 40 ? "high" : sharePct >= 15 ? "medium" : "low";
    return { hours, text: `≈ ${hours.toLocaleString("en-US")}h at stake`, sharePct, level };
  }

  const level = hours >= 80 ? "high" : hours >= 20 ? "medium" : "low";
  return { hours, text: `≈ ${hours.toLocaleString("en-US")}h at stake`, level };
}

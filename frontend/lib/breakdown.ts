/** Helpers for rendering a PhaseEstimate's structured `breakdown` (a map of the
 *  algorithm's numeric components) graphically on the review page. */

export interface BreakdownMetric {
  key: string;
  value: number;
}

/** Turn the breakdown map into an ordered list of metrics. */
export function toMetrics(breakdown: Record<string, number>): BreakdownMetric[] {
  return Object.entries(breakdown).map(([key, value]) => ({ key, value }));
}

/** True when a metric represents an hours quantity (gets a magnitude bar), as
 *  opposed to a rate/multiplier/percentage parameter (gets a chip). */
export function isHoursMetric(key: string): boolean {
  const k = key.toLowerCase();
  if (/per[_ ]?hr|loc_per|rate/.test(k)) return false;
  return /hours?|hrs/.test(k);
}

/** "review_hours_pre_tooling" → "Review hours pre tooling". */
export function humanizeKey(key: string): string {
  const s = key.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Format a metric value with a unit inferred from its key. */
export function formatMetricValue(key: string, value: number): string {
  const k = key.toLowerCase();
  const num = Number.isInteger(value)
    ? value.toLocaleString()
    : value.toFixed(1);
  if (/multiplier|factor|modifier/.test(k)) return `×${value}`;
  if (/pct|percent/.test(k)) return `${value}%`;
  if (isHoursMetric(k)) return `${num} h`;
  return num;
}

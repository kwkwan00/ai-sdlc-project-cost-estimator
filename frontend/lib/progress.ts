/** Pure helpers for a time-based "trickle" progress bar.
 *
 *  Some operations (the WBS planner LLM call) are a single opaque request with no sub-progress to
 *  stream, so a truthful 0–100 bar isn't possible. Instead the bar advances on elapsed time toward
 *  a ceiling < 100, decelerating as it approaches so it never *claims* completion on time alone —
 *  the caller jumps it to 100 only when the real work finishes. This is the same illusion NProgress
 *  uses; keeping the math here (deterministic, no `Math.random`) makes it unit-testable. */

/** How often the caller should tick `trickle`, in ms. */
export const PROGRESS_TICK_MS = 350;

/**
 * Advance `current` (0–100) a fraction of the remaining distance to `ceiling`.
 *
 * The multiplicative `rate` makes each step smaller than the last (so the bar visibly slows as it
 * nears the ceiling — the "almost done" feel), while `minStep` floors the step so a long operation
 * never looks frozen. The result is clamped to `ceiling`, so on time alone the bar approaches but
 * stops there; the caller sets 100 when the request resolves.
 */
export function trickle(current: number, ceiling: number, rate = 0.08, minStep = 0.4): number {
  if (current >= ceiling) return ceiling;
  const step = Math.max((ceiling - current) * rate, minStep);
  return Math.min(ceiling, current + step);
}

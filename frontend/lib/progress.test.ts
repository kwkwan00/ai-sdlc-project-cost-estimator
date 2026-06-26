import { describe, expect, it } from "vitest";

import { PROGRESS_TICK_MS, trickle } from "./progress";

describe("trickle", () => {
  it("advances toward the ceiling and never exceeds it", () => {
    expect(trickle(0, 90)).toBeGreaterThan(0);
    expect(trickle(89.99, 90)).toBeLessThanOrEqual(90);
    expect(trickle(50, 90)).toBeLessThanOrEqual(90);
  });

  it("strictly increases while below the ceiling", () => {
    let p = 0;
    for (let i = 0; i < 12; i++) {
      const next = trickle(p, 90);
      expect(next).toBeGreaterThan(p);
      p = next;
    }
  });

  it("decelerates as it approaches the ceiling (each step smaller than the last)", () => {
    const step1 = trickle(0, 90) - 0;
    const p1 = trickle(0, 90);
    const step2 = trickle(p1, 90) - p1;
    expect(step2).toBeLessThan(step1);
  });

  it("keeps a visible minimum step instead of freezing near the ceiling", () => {
    // Pure multiplicative decay would step 0.1 * 0.08 = 0.008 here; the floor lifts it.
    expect(trickle(89.9, 90, 0.08, 0.4)).toBe(90); // clamped to ceiling, not 90.3
    expect(trickle(80, 90, 0.08, 0.4) - 80).toBeGreaterThanOrEqual(0.4);
  });

  it("returns the ceiling once reached or exceeded (no overshoot, no NaN)", () => {
    expect(trickle(90, 90)).toBe(90);
    expect(trickle(95, 90)).toBe(90);
  });

  it("converges to the ceiling over many ticks", () => {
    let p = 0;
    for (let i = 0; i < 300; i++) p = trickle(p, 90);
    expect(p).toBe(90);
  });

  it("exposes a sane tick interval", () => {
    expect(PROGRESS_TICK_MS).toBeGreaterThan(0);
  });
});

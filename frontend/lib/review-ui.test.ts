import { describe, expect, it } from "vitest";

import { confidenceLevel, reconciledTotals, sharePct } from "./review-ui";

describe("confidenceLevel", () => {
  it("buckets 0..1 into low/medium/high", () => {
    expect(confidenceLevel(0)).toBe("low");
    expect(confidenceLevel(0.49)).toBe("low");
    expect(confidenceLevel(0.5)).toBe("medium");
    expect(confidenceLevel(0.74)).toBe("medium");
    expect(confidenceLevel(0.75)).toBe("high");
    expect(confidenceLevel(1)).toBe("high");
  });
});

describe("sharePct", () => {
  it("computes a whole-percentage share", () => {
    expect(sharePct(25, 100)).toBe(25);
    expect(sharePct(1, 3)).toBe(33);
  });

  it("guards against a zero/negative total", () => {
    expect(sharePct(10, 0)).toBe(0);
    expect(sharePct(10, -5)).toBe(0);
  });
});

describe("reconciledTotals", () => {
  it("derives saved so AI + saved === manual exactly", () => {
    const t = reconciledTotals({
      aiHours: 9000,
      manualHours: 10000,
      aiCost: 800000,
      manualCost: 1000000,
    });
    expect(t.savedHours).toBe(1000);
    expect(t.aiHours + t.savedHours).toBe(t.manualHours);
    expect(t.savedCost).toBe(200000);
    expect(t.aiCost + t.savedCost).toBe(t.manualCost);
  });

  it("rounds first, so no ±1 drift between the three figures", () => {
    // ai 8999.6→9000, manual 9999.4→9999 → saved must be 999 (not 1000), and add up.
    const t = reconciledTotals({
      aiHours: 8999.6,
      manualHours: 9999.4,
      aiCost: 0,
      manualCost: 0,
    });
    expect(t.aiHours).toBe(9000);
    expect(t.manualHours).toBe(9999);
    expect(t.savedHours).toBe(999);
    expect(t.aiHours + t.savedHours).toBe(t.manualHours);
  });
});

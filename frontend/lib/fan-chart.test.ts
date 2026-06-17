import { describe, expect, it } from "vitest";

import {
  confidenceLabel,
  hasPercentiles,
  pAiSavesTime,
  toFanSeries,
} from "./fan-chart";
import type { HourRange } from "./types";

/** A fully-simulated range with the seven-percentile ladder. */
function simulated(overrides: Partial<HourRange> = {}): HourRange {
  return {
    optimistic: 8_400,
    most_likely: 10_000,
    pessimistic: 12_900,
    std: 1_400,
    mean: 10_200,
    percentiles: {
      p5: 7_800,
      p10: 8_400,
      p25: 9_200,
      p50: 10_100,
      p75: 11_000,
      p90: 12_900,
      p95: 13_700,
    },
    ...overrides,
  };
}

/** A legacy/stub range with only the three deterministic points. */
function legacy(): HourRange {
  return { optimistic: 800, most_likely: 1_000, pessimistic: 1_500 };
}

describe("hasPercentiles", () => {
  it("is true for the full p5..p95 ladder", () => {
    expect(hasPercentiles(simulated())).toBe(true);
  });

  it("is false when percentiles are absent", () => {
    expect(hasPercentiles(legacy())).toBe(false);
  });

  it("is false for a partial/non-finite ladder", () => {
    expect(
      hasPercentiles({
        optimistic: 1,
        most_likely: 2,
        pessimistic: 3,
        // missing p95
        percentiles: { p5: 1, p10: 1, p25: 1, p50: 2, p75: 3, p90: 3 },
      }),
    ).toBe(false);
    expect(
      hasPercentiles({
        optimistic: 1,
        most_likely: 2,
        pessimistic: 3,
        percentiles: {
          p5: Number.NaN,
          p10: 1,
          p25: 1,
          p50: 2,
          p75: 3,
          p90: 3,
          p95: 3,
        },
      }),
    ).toBe(false);
  });
});

describe("toFanSeries", () => {
  it("maps percentiles to nested P5–P95 / P10–P90 bands + median", () => {
    const series = toFanSeries(simulated(), "AI-assisted");
    expect(series).toHaveLength(1);
    const row = series[0];
    expect(row.label).toBe("AI-assisted");
    expect(row.simulated).toBe(true);
    expect(row.outer).toEqual([7_800, 13_700]); // P5..P95
    expect(row.inner).toEqual([8_400, 12_900]); // P10..P90
    expect(row.mid).toBe(10_000); // deterministic most_likely
    expect(row.p50).toBe(10_100); // simulated median
  });

  it("falls back to a 3-point band when percentiles are absent", () => {
    const series = toFanSeries(legacy());
    expect(series).toHaveLength(1);
    const row = series[0];
    expect(row.label).toBe("Hours"); // default label
    expect(row.simulated).toBe(false);
    // Both bands collapse to optimistic→pessimistic.
    expect(row.outer).toEqual([800, 1_500]);
    expect(row.inner).toEqual([800, 1_500]);
    expect(row.mid).toBe(1_000);
    expect(row.p50).toBeUndefined();
  });
});

describe("confidenceLabel", () => {
  it("quotes an 80% interval from P10–P90 when simulated", () => {
    expect(confidenceLabel(simulated())).toBe("80% confident: 8,400–12,900 h");
  });

  it("degrades to an estimated range (no % claim) without percentiles", () => {
    expect(confidenceLabel(legacy())).toBe("Estimated range: 800–1,500 h");
  });
});

describe("pAiSavesTime", () => {
  it("returns 1 when AI is a clear win at every percentile", () => {
    const ai = simulated({
      percentiles: {
        p5: 100,
        p10: 200,
        p25: 300,
        p50: 400,
        p75: 500,
        p90: 600,
        p95: 700,
      },
    });
    const manual = simulated({
      percentiles: {
        p5: 1_000,
        p10: 1_100,
        p25: 1_200,
        p50: 1_300,
        p75: 1_400,
        p90: 1_500,
        p95: 1_600,
      },
    });
    expect(pAiSavesTime(ai, manual)).toBe(1);
  });

  it("returns a fraction in (0,1) when the distributions overlap", () => {
    // AI wins at the 4 lower percentiles, loses at the 3 upper ones → 4/7.
    const ai = simulated({
      percentiles: {
        p5: 100,
        p10: 200,
        p25: 300,
        p50: 400,
        p75: 900,
        p90: 1_000,
        p95: 1_100,
      },
    });
    const manual = simulated({
      percentiles: {
        p5: 150,
        p10: 250,
        p25: 350,
        p50: 450,
        p75: 500,
        p90: 600,
        p95: 700,
      },
    });
    const p = pAiSavesTime(ai, manual);
    expect(p).toBeCloseTo(4 / 7, 10);
    expect(p! > 0 && p! < 1).toBe(true);
  });

  it("counts exact ties as half-wins", () => {
    const same = simulated();
    // Every matched percentile is equal → 7 * 0.5 / 7 = 0.5.
    expect(pAiSavesTime(same, simulated())).toBe(0.5);
  });

  it("returns null when either side lacks percentiles", () => {
    expect(pAiSavesTime(legacy(), simulated())).toBeNull();
    expect(pAiSavesTime(simulated(), legacy())).toBeNull();
    expect(pAiSavesTime(legacy(), legacy())).toBeNull();
  });
});

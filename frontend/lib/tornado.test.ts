import { describe, expect, it } from "vitest";

import { buildTornado, phaseSpread, Z80 } from "./tornado";
import type { HourRange, Phase, PhaseEstimate } from "./types";

/** A simulated HourRange with the full percentile ladder. */
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

/** A minimal PhaseEstimate carrying only the fields the tornado helpers read. */
function phase(
  name: Phase,
  ai: HourRange,
  manual: HourRange = ai,
): PhaseEstimate {
  return {
    phase: name,
    twin_name: name,
    algorithm: "ucp",
    ai_assisted_hours: ai,
    manual_only_hours: manual,
    ai_assisted_role_hours: [],
    manual_only_role_hours: [],
    assumptions: [],
    risks: [],
    confidence: 0.7,
    breakdown: {},
    effective_ai_reduction_pct: 30,
    notes: "",
  };
}

describe("phaseSpread", () => {
  it("uses the exact P10–P90 width when percentiles are present", () => {
    const row = phaseSpread(phase("development", simulated()), "ai_assisted");
    expect(row.simulated).toBe(true);
    expect(row.low).toBe(8_400); // p10
    expect(row.high).toBe(12_900); // p90
    expect(row.spread).toBe(4_500); // p90 − p10
    expect(row.mid).toBe(10_000);
    expect(row.label).toBe("Development");
  });

  it("derives an 80% band from std (centered on most_likely) when no percentiles", () => {
    const range: HourRange = {
      optimistic: 900,
      most_likely: 1_000,
      pessimistic: 1_200,
      std: 100,
    };
    const row = phaseSpread(phase("qa_testing", range), "ai_assisted");
    expect(row.simulated).toBe(true);
    const half = Z80 * 100;
    expect(row.low).toBeCloseTo(1_000 - half, 6);
    expect(row.high).toBeCloseTo(1_000 + half, 6);
    expect(row.spread).toBeCloseTo(2 * half, 6);
  });

  it("falls back to pessimistic − optimistic with no percentiles or std", () => {
    const range: HourRange = {
      optimistic: 800,
      most_likely: 1_000,
      pessimistic: 1_500,
    };
    const row = phaseSpread(phase("discovery", range), "ai_assisted");
    expect(row.simulated).toBe(false);
    expect(row.low).toBe(800);
    expect(row.high).toBe(1_500);
    expect(row.spread).toBe(700);
  });

  it("reads the manual_only range when mode is manual_only", () => {
    const ai = simulated({ percentiles: { ...simulated().percentiles!, p10: 8_000, p90: 9_000 } });
    const manual = simulated({ percentiles: { ...simulated().percentiles!, p10: 6_000, p90: 16_000 } });
    const row = phaseSpread(phase("development", ai, manual), "manual_only");
    expect(row.spread).toBe(10_000); // 16,000 − 6,000 from the manual range
  });

  it("never reports a negative spread", () => {
    const range: HourRange = {
      optimistic: 1_500,
      most_likely: 1_000,
      pessimistic: 800, // inverted on purpose
    };
    const row = phaseSpread(phase("deployment", range), "ai_assisted");
    expect(row.spread).toBe(0);
  });
});

describe("buildTornado", () => {
  it("ranks phases by spread, largest first, with shares summing to ~1", () => {
    const small = { optimistic: 90, most_likely: 100, pessimistic: 110 }; // spread 20
    const mid = { optimistic: 80, most_likely: 100, pessimistic: 180 }; // spread 100
    const big = { optimistic: 50, most_likely: 100, pessimistic: 350 }; // spread 300
    const rows = buildTornado(
      [
        phase("discovery", small),
        phase("development", big),
        phase("qa_testing", mid),
      ],
      "ai_assisted",
    );
    expect(rows.map((r) => r.phase)).toEqual([
      "development",
      "qa_testing",
      "discovery",
    ]);
    expect(rows.map((r) => r.spread)).toEqual([300, 100, 20]);
    const totalShare = rows.reduce((s, r) => s + r.share, 0);
    expect(totalShare).toBeCloseTo(1, 10);
    expect(rows[0].share).toBeCloseTo(300 / 420, 10);
  });

  it("is stable for equal spreads (preserves input order)", () => {
    const r1 = { optimistic: 0, most_likely: 50, pessimistic: 100 };
    const r2 = { optimistic: 0, most_likely: 50, pessimistic: 100 };
    const rows = buildTornado(
      [phase("discovery", r1), phase("ux_design", r2)],
      "ai_assisted",
    );
    expect(rows.map((r) => r.phase)).toEqual(["discovery", "ux_design"]);
  });

  it("gives a zero share to every phase when total spread is zero", () => {
    const flat = { optimistic: 100, most_likely: 100, pessimistic: 100 };
    const rows = buildTornado([phase("discovery", flat)], "ai_assisted");
    expect(rows[0].spread).toBe(0);
    expect(rows[0].share).toBe(0);
  });

  it("returns an empty array for no phases", () => {
    expect(buildTornado([], "ai_assisted")).toEqual([]);
  });
});

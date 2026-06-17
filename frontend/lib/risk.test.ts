import { describe, expect, it } from "vitest";

import {
  collectPhaseRisks,
  expectedRiskHours,
  impactMidpoint,
  sortRisks,
} from "./risk";
import type { HourRange, Phase, PhaseEstimate, Risk } from "./types";

function risk(overrides: Partial<Risk> = {}): Risk {
  return {
    description: "Some risk",
    likelihood: 0.5,
    impact_hours_low: 100,
    impact_hours_high: 300,
    ...overrides,
  };
}

const ZERO_RANGE: HourRange = { optimistic: 0, most_likely: 0, pessimistic: 0 };

function phase(name: Phase, risks: Risk[]): PhaseEstimate {
  return {
    phase: name,
    twin_name: name,
    algorithm: "ucp",
    ai_assisted_hours: ZERO_RANGE,
    manual_only_hours: ZERO_RANGE,
    ai_assisted_role_hours: [],
    manual_only_role_hours: [],
    assumptions: [],
    risks,
    confidence: 0.7,
    breakdown: {},
    effective_ai_reduction_pct: 0,
    notes: "",
  };
}

describe("impactMidpoint", () => {
  it("averages the low/high impact bounds", () => {
    expect(impactMidpoint(risk({ impact_hours_low: 100, impact_hours_high: 300 }))).toBe(200);
  });
});

describe("expectedRiskHours", () => {
  it("multiplies likelihood by the impact midpoint", () => {
    expect(
      expectedRiskHours(risk({ likelihood: 0.5, impact_hours_low: 100, impact_hours_high: 300 })),
    ).toBe(100); // 0.5 × 200
  });

  it("is zero when likelihood is zero", () => {
    expect(expectedRiskHours(risk({ likelihood: 0 }))).toBe(0);
  });
});

describe("sortRisks", () => {
  it("orders by expected impact, highest first, without mutating input", () => {
    const low = risk({ likelihood: 0.1, impact_hours_low: 10, impact_hours_high: 30 }); // exp 2
    const high = risk({ likelihood: 0.9, impact_hours_low: 200, impact_hours_high: 400 }); // exp 270
    const mid = risk({ likelihood: 0.5, impact_hours_low: 100, impact_hours_high: 300 }); // exp 100
    const input = [low, high, mid];
    const sorted = sortRisks(input);
    expect(sorted.map((r) => r.likelihood)).toEqual([0.9, 0.5, 0.1]);
    // Input array order is preserved (pure).
    expect(input.map((r) => r.likelihood)).toEqual([0.1, 0.9, 0.5]);
  });

  it("is stable on ties", () => {
    const a = risk({ description: "a", likelihood: 0.5, impact_hours_low: 100, impact_hours_high: 100 });
    const b = risk({ description: "b", likelihood: 0.5, impact_hours_low: 100, impact_hours_high: 100 });
    expect(sortRisks([a, b]).map((r) => r.description)).toEqual(["a", "b"]);
  });
});

describe("collectPhaseRisks", () => {
  it("flattens risks across phases, tags the phase, and sorts by expected impact", () => {
    const phases = [
      phase("discovery", [
        risk({ description: "d1", likelihood: 0.2, impact_hours_low: 50, impact_hours_high: 50 }), // exp 10
      ]),
      phase("development", [
        risk({ description: "dev1", likelihood: 0.8, impact_hours_low: 400, impact_hours_high: 600 }), // exp 400
        risk({ description: "dev2", likelihood: 0.3, impact_hours_low: 100, impact_hours_high: 100 }), // exp 30
      ]),
    ];
    const out = collectPhaseRisks(phases);
    expect(out).toHaveLength(3);
    expect(out.map((x) => x.risk.description)).toEqual(["dev1", "dev2", "d1"]);
    expect(out.map((x) => x.phase)).toEqual(["development", "development", "discovery"]);
    expect(out[0].expectedHours).toBe(400);
  });

  it("returns an empty array when no phase has risks", () => {
    expect(collectPhaseRisks([phase("discovery", []), phase("qa_testing", [])])).toEqual([]);
  });
});

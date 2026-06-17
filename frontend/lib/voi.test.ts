import { describe, expect, it } from "vitest";

import { questionImpact, rankQuestions, totalImpact, voiLabel } from "./voi";
import type { ClarifyingQuestion } from "./types";

function q(overrides: Partial<ClarifyingQuestion> = {}): ClarifyingQuestion {
  return {
    id: "q1",
    text: "Which auth provider?",
    source_phases: ["development"],
    suggested_default: "Auth0",
    impact_hours: 40,
    answered: false,
    answer: null,
    ...overrides,
  };
}

describe("questionImpact", () => {
  it("returns the impact_hours when positive and finite", () => {
    expect(questionImpact(q({ impact_hours: 120 }))).toBe(120);
  });

  it("floors negative / NaN / zero impact at 0", () => {
    expect(questionImpact(q({ impact_hours: -5 }))).toBe(0);
    expect(questionImpact(q({ impact_hours: Number.NaN }))).toBe(0);
    expect(questionImpact(q({ impact_hours: 0 }))).toBe(0);
  });
});

describe("totalImpact", () => {
  it("sums the (floored) impacts", () => {
    expect(
      totalImpact([q({ impact_hours: 40 }), q({ impact_hours: 60 }), q({ impact_hours: -10 })]),
    ).toBe(100);
  });
});

describe("rankQuestions", () => {
  it("orders by impact, highest first, without mutating input", () => {
    const a = q({ id: "a", impact_hours: 10 });
    const b = q({ id: "b", impact_hours: 200 });
    const c = q({ id: "c", impact_hours: 50 });
    const input = [a, b, c];
    const ranked = rankQuestions(input);
    expect(ranked.map((x) => x.id)).toEqual(["b", "c", "a"]);
    expect(input.map((x) => x.id)).toEqual(["a", "b", "c"]); // pure
  });

  it("is stable on ties (keeps backend order)", () => {
    const a = q({ id: "a", impact_hours: 30 });
    const b = q({ id: "b", impact_hours: 30 });
    expect(rankQuestions([a, b]).map((x) => x.id)).toEqual(["a", "b"]);
  });
});

describe("voiLabel", () => {
  it("reports hours-at-stake and an absolute tier without a total", () => {
    expect(voiLabel(q({ impact_hours: 120 }))).toMatchObject({
      hours: 120,
      text: "≈ 120h at stake",
      level: "high",
    });
    expect(voiLabel(q({ impact_hours: 40 })).level).toBe("medium");
    expect(voiLabel(q({ impact_hours: 5 })).level).toBe("low");
  });

  it("computes a relative share and tier when a positive total is supplied", () => {
    // 60 of 100 total → 60% share → high.
    const badge = voiLabel(q({ impact_hours: 60 }), 100);
    expect(badge.sharePct).toBe(60);
    expect(badge.level).toBe("high");
    expect(badge.text).toBe("≈ 60h at stake");
  });

  it("tiers a small relative share as low even when absolute hours are large", () => {
    // 100h is absolutely large, but only 10% of a 1000h total → low.
    const badge = voiLabel(q({ impact_hours: 100 }), 1_000);
    expect(badge.sharePct).toBe(10);
    expect(badge.level).toBe("low");
  });

  it("marks a question with no usable impact as unknown", () => {
    expect(voiLabel(q({ impact_hours: 0 }))).toEqual({
      hours: 0,
      text: "impact unknown",
      level: "none",
    });
  });

  it("ignores a zero/negative total and falls back to absolute tiering", () => {
    const badge = voiLabel(q({ impact_hours: 90 }), 0);
    expect(badge.sharePct).toBeUndefined();
    expect(badge.level).toBe("high");
  });
});

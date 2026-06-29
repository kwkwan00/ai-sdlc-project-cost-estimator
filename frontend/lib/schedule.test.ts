import { describe, expect, it } from "vitest";

import { deriveSchedule, PHASE_ORDER } from "./schedule";
import type { DualScenarioEstimate, HourRange, Phase, PhaseEstimate } from "./types";

function mkRange(m: number, withPercentiles = false): HourRange {
  const o = m * 0.8;
  const p = m * 1.35;
  const base: HourRange = { optimistic: o, most_likely: m, pessimistic: p };
  if (!withPercentiles) return base;
  return {
    ...base,
    std: m * 0.15,
    mean: m * 1.02,
    percentiles: {
      p5: m * 0.78,
      p10: o,
      p25: m * 0.92,
      p50: m,
      p75: m * 1.12,
      p90: p,
      p95: m * 1.4,
    },
  };
}

function mkPhase(phase: Phase, mlHours: number, withPercentiles = false): PhaseEstimate {
  const r = mkRange(mlHours, withPercentiles);
  return {
    phase,
    twin_name: `${phase}_twin`,
    algorithm: "test",
    ai_assisted_hours: r,
    manual_only_hours: mkRange(mlHours * 1.3, withPercentiles),
    ai_assisted_role_hours: [],
    manual_only_role_hours: [],
    assumptions: [],
    risks: [],
    confidence: 0.7,
    breakdown: {},
    effective_ai_reduction_pct: 23,
    notes: "",
  };
}

/** Dev-heavy six-phase estimate (development dominates → on the critical path). */
function mkEstimate(withPercentiles = false, low = 16, high = 24): DualScenarioEstimate {
  const hours: Record<Phase, number> = {
    discovery: 200,
    ux_design: 300,
    development: 4000,
    code_review: 250,
    qa_testing: 900,
    deployment: 400,
  };
  return {
    total_ai_assisted_hours: mkRange(6050, withPercentiles),
    total_manual_only_hours: mkRange(7800, withPercentiles),
    ai_hours_saved_pert: 1750,
    ai_cost_saved_usd: 0,
    phases: PHASE_ORDER.map((p) => mkPhase(p, hours[p], withPercentiles)),
    confidence: 0.7,
    duration_weeks_low: low,
    duration_weeks_high: high,
    headcount_by_role: [],
    weekly_burn_rate_usd: 0,
    team_size: 8,
    optimal_team_size: 8,
    total_cost_ai_assisted_usd: 0,
    total_cost_manual_only_usd: 0,
    llm_usage: { call_count: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cost_usd: 0, by_model: [], by_agent: [] },
  };
}

describe("deriveSchedule — layout", () => {
  it("orders phases and overlaps them (design starts before discovery ends)", () => {
    const s = deriveSchedule(mkEstimate(), "ai_assisted");
    const byPhase = Object.fromEntries(s.phases.map((p) => [p.phase, p]));
    expect(byPhase.discovery.startWeek).toBe(0);
    // ux starts at 0.6·discovery (overlap), i.e. before discovery finishes.
    expect(byPhase.ux_design.startWeek).toBeGreaterThan(0);
    expect(byPhase.ux_design.startWeek).toBeLessThan(byPhase.discovery.endWeek);
    // deployment is last.
    const maxEnd = Math.max(...s.phases.map((p) => p.endWeek));
    expect(byPhase.deployment.endWeek).toBeCloseTo(maxEnd, 6);
  });

  it("scales the span to the estimate's reported duration (midpoint of the band)", () => {
    const s = deriveSchedule(mkEstimate(false, 16, 24), "ai_assisted");
    expect(s.totalWeeks).toBeCloseTo(20, 6); // (16+24)/2
  });

  it("effort shares are non-negative and sum to ~1", () => {
    const s = deriveSchedule(mkEstimate(), "ai_assisted");
    const sum = s.phases.reduce((a, p) => a + p.effortShare, 0);
    expect(sum).toBeCloseTo(1, 6);
    expect(s.phases.every((p) => p.effortShare >= 0)).toBe(true);
  });
});

describe("deriveSchedule — critical path & slack", () => {
  it("puts development on the critical path and gives code_review slack", () => {
    const s = deriveSchedule(mkEstimate(), "ai_assisted");
    expect(s.criticalPath).toContain("development");
    const byPhase = Object.fromEntries(s.phases.map((p) => [p.phase, p]));
    expect(byPhase.development.isCritical).toBe(true);
    expect(byPhase.development.slackWeeks).toBeCloseTo(0, 6);
    // code_review runs parallel to the longer qa_testing → it has slack.
    expect(byPhase.code_review.isCritical).toBe(false);
    expect(byPhase.code_review.slackWeeks).toBeGreaterThan(0);
  });

  it("all slack is non-negative", () => {
    const s = deriveSchedule(mkEstimate(), "ai_assisted");
    expect(s.phases.every((p) => p.slackWeeks >= -1e-9)).toBe(true);
  });
});

describe("deriveSchedule — milestones", () => {
  it("emits Kickoff at week 0 and Launch at the project end, in order", () => {
    const s = deriveSchedule(mkEstimate(), "ai_assisted");
    expect(s.milestones[0]).toMatchObject({ name: "Kickoff", week: 0, kind: "kickoff" });
    const launch = s.milestones.find((m) => m.kind === "launch");
    expect(launch?.name).toBe("Launch");
    expect(launch?.week).toBeCloseTo(s.totalWeeks, 6);
    const weeks = s.milestones.map((m) => m.week);
    expect([...weeks].sort((a, b) => a - b)).toEqual(weeks); // already ascending
  });
});

describe("deriveSchedule — Monte-Carlo risk overlay", () => {
  it("simulates from real percentiles: criticality bounded, P10≤median≤P90, CDF monotone 0→1", () => {
    const s = deriveSchedule(mkEstimate(true), "ai_assisted", { draws: 400, seed: 7 });
    const risk = s.risk!;
    expect(risk.simulated).toBe(true);
    expect(s.phases.every((p) => p.criticalityPct >= 0 && p.criticalityPct <= 100)).toBe(true);
    // dev dominates → very high criticality.
    const dev = s.phases.find((p) => p.phase === "development")!;
    expect(dev.criticalityPct).toBeGreaterThan(80);
    expect(risk.p10Weeks).toBeLessThanOrEqual(risk.medianWeeks);
    expect(risk.medianWeeks).toBeLessThanOrEqual(risk.p90Weeks);
    expect(risk.pFinishBy(0)).toBe(0);
    expect(risk.pFinishBy(risk.p90Weeks * 10)).toBe(1);
    expect(risk.pFinishBy(risk.p10Weeks)).toBeLessThan(risk.pFinishBy(risk.p90Weeks));
  });

  it("falls back to a triangular sample when percentiles are absent", () => {
    const s = deriveSchedule(mkEstimate(false), "ai_assisted", { draws: 200, seed: 3 });
    expect(s.risk).not.toBeNull();
    expect(s.risk!.simulated).toBe(false);
    expect(s.risk!.medianWeeks).toBeGreaterThan(0);
  });

  it("is deterministic for a fixed seed", () => {
    const a = deriveSchedule(mkEstimate(true), "ai_assisted", { draws: 300, seed: 42 });
    const b = deriveSchedule(mkEstimate(true), "ai_assisted", { draws: 300, seed: 42 });
    expect(a.risk!.medianWeeks).toBe(b.risk!.medianWeeks);
    expect(a.phases.map((p) => p.criticalityPct)).toEqual(b.phases.map((p) => p.criticalityPct));
  });
});

describe("deriveSchedule — degenerate inputs", () => {
  it("returns an empty schedule (no risk) when there are no hours", () => {
    const fe = mkEstimate();
    fe.phases = fe.phases.map((p) => ({
      ...p,
      ai_assisted_hours: { optimistic: 0, most_likely: 0, pessimistic: 0 },
    }));
    const s = deriveSchedule(fe, "ai_assisted");
    expect(s.totalWeeks).toBe(0);
    expect(s.criticalPath).toEqual([]);
    expect(s.risk).toBeNull();
  });

  it("handles a missing phase by collapsing it to zero duration", () => {
    const fe = mkEstimate();
    fe.phases = fe.phases.filter((p) => p.phase !== "code_review");
    const s = deriveSchedule(fe, "ai_assisted");
    const cr = s.phases.find((p) => p.phase === "code_review")!;
    expect(cr.durationWeeks).toBe(0);
    expect(s.phases).toHaveLength(6); // still emits all six rows
  });
});

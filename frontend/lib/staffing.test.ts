import { describe, expect, it } from "vitest";

import { staffingSummary } from "./staffing";
import type { DualScenarioEstimate } from "./types";

function fe(partial: Partial<DualScenarioEstimate>): DualScenarioEstimate {
  return partial as DualScenarioEstimate;
}

describe("staffingSummary", () => {
  it("is absent for a legacy estimate without the fields", () => {
    const s = staffingSummary(fe({}));
    expect(s.present).toBe(false);
    expect(s.staffing).toBe("balanced");
  });

  it("classifies a team above the sweet spot as overstaffed", () => {
    const s = staffingSummary(
      fe({
        brooks_overhead_pct: 21,
        staffing_efficiency_pct: 78,
        team_size: 9,
        optimal_team_size: 6,
      }),
    );
    expect(s.present).toBe(true);
    expect(s.staffing).toBe("overstaffed");
    expect(s.label).toBe(
      "+21% coordination overhead · 78% scaling efficiency · sweet spot ≈ 6",
    );
  });

  it("classifies a team below the sweet spot as understaffed", () => {
    const s = staffingSummary(fe({ team_size: 2, optimal_team_size: 6 }));
    expect(s.staffing).toBe("understaffed");
  });

  it("treats a within-one-head gap as balanced", () => {
    const s = staffingSummary(fe({ team_size: 6, optimal_team_size: 6 }));
    expect(s.staffing).toBe("balanced");
    const near = staffingSummary(fe({ team_size: 7, optimal_team_size: 6 }));
    expect(near.staffing).toBe("balanced");
  });

  it("rounds the efficiency but keeps the overhead percentage verbatim", () => {
    const s = staffingSummary(
      fe({
        brooks_overhead_pct: 12.5,
        staffing_efficiency_pct: 77.6,
        team_size: 8,
        optimal_team_size: 7,
      }),
    );
    expect(s.label).toContain("+12.5% coordination overhead");
    expect(s.label).toContain("78% scaling efficiency");
  });
});

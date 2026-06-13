import { describe, expect, it } from "vitest";

import {
  formatMetricValue,
  humanizeKey,
  isHoursMetric,
  toMetrics,
} from "./breakdown";

describe("toMetrics", () => {
  it("turns the structured breakdown map into ordered metrics", () => {
    const metrics = toMetrics({
      inspection_rate_loc_per_hr: 210,
      review_hours_pre_tooling: 232.9,
      rework_multiplier: 1.14,
      tooling_setup_hours: 12,
    });
    expect(metrics).toEqual([
      { key: "inspection_rate_loc_per_hr", value: 210 },
      { key: "review_hours_pre_tooling", value: 232.9 },
      { key: "rework_multiplier", value: 1.14 },
      { key: "tooling_setup_hours", value: 12 },
    ]);
  });
});

describe("isHoursMetric", () => {
  it("flags hour quantities but not rates/multipliers", () => {
    expect(isHoursMetric("review_hours_pre_tooling")).toBe(true);
    expect(isHoursMetric("tooling_setup_hours")).toBe(true);
    expect(isHoursMetric("plan_a_hours")).toBe(true);
    expect(isHoursMetric("inspection_rate_loc_per_hr")).toBe(false);
    expect(isHoursMetric("rework_multiplier")).toBe(false);
    expect(isHoursMetric("conservative_bias_pct")).toBe(false);
    expect(isHoursMetric("total_tp")).toBe(false);
  });
});

describe("humanizeKey", () => {
  it("turns snake_case into a sentence", () => {
    expect(humanizeKey("review_hours_pre_tooling")).toBe("Review hours pre tooling");
  });
});

describe("formatMetricValue", () => {
  it("infers a unit from the key", () => {
    expect(formatMetricValue("review_hours_pre_tooling", 232.9)).toBe("232.9 h");
    expect(formatMetricValue("tooling_setup_hours", 12)).toBe("12 h");
    expect(formatMetricValue("rework_multiplier", 1.14)).toBe("×1.14");
    expect(formatMetricValue("responsive_modifier", 1.35)).toBe("×1.35");
    expect(formatMetricValue("conservative_bias_pct", 12)).toBe("12%");
    expect(formatMetricValue("inspection_rate_loc_per_hr", 210)).toBe("210");
  });
});

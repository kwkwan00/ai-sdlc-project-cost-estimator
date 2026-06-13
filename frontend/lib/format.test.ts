import { describe, expect, it } from "vitest";

import {
  formatHours,
  formatPct,
  formatTokens,
  formatUSD,
  formatUSDPrecise,
} from "./format";

describe("formatHours", () => {
  it("rounds and appends 'h'", () => {
    expect(formatHours(123.4)).toBe("123 h");
    expect(formatHours(123.7)).toBe("124 h");
  });

  it("uses thousands separators for large values", () => {
    expect(formatHours(12345)).toBe("12,345 h");
  });
});

describe("formatUSD", () => {
  it("formats as US dollars with no fractional digits", () => {
    expect(formatUSD(12345)).toBe("$12,345");
    expect(formatUSD(0)).toBe("$0");
  });
});

describe("formatPct", () => {
  it("renders a 0..1 fraction as a percentage", () => {
    expect(formatPct(0.07)).toBe("7%");
    expect(formatPct(0.5)).toBe("50%");
    expect(formatPct(1)).toBe("100%");
  });
});

describe("formatUSDPrecise", () => {
  it("shows cents for small amounts (LLM cost) and whole dollars for large", () => {
    expect(formatUSDPrecise(0.6111)).toBe("$0.61");
    expect(formatUSDPrecise(0.0051)).toBe("$0.01");
    expect(formatUSDPrecise(0)).toBe("$0.00");
    expect(formatUSDPrecise(12345)).toBe("$12,345");
  });
});

describe("formatTokens", () => {
  it("renders compact token counts", () => {
    expect(formatTokens(842)).toBe("842");
    expect(formatTokens(58239)).toBe("58.2k");
    expect(formatTokens(13609)).toBe("13.6k");
    expect(formatTokens(1_200_000)).toBe("1.2M");
  });
});

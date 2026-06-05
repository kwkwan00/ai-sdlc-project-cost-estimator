import { describe, expect, it } from "vitest";

import { formatHours, formatPct, formatUSD } from "./format";

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

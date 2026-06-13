import { describe, expect, it } from "vitest";

import { algorithmColor, algorithmInfo } from "./algorithms";

describe("algorithmInfo", () => {
  it("resolves each twin's real algorithm string to its description", () => {
    // The exact `algorithm` values the six twins emit.
    expect(algorithmInfo("UCP")?.name).toBe("Use Case Points (UCP)");
    expect(algorithmInfo("SCP")?.name).toBe("Screen Complexity Points (SCP)");
    expect(algorithmInfo("COCOMO_II")?.name).toBe("COCOMO II");
    expect(algorithmInfo("Fagan")?.name).toBe("Fagan inspection");
    expect(algorithmInfo("CMP")?.name).toBe("Cloud Migration Points (CMP)");
    // TPA plan variants all map to Test Point Analysis.
    expect(algorithmInfo("TPA_Plan_A")?.name).toBe("Test Point Analysis (TPA)");
    expect(algorithmInfo("TPA_Plan_B")?.name).toBe("Test Point Analysis (TPA)");
    expect(algorithmInfo("TPA_Plan_C")?.name).toBe("Test Point Analysis (TPA)");
  });

  it("is case-insensitive", () => {
    expect(algorithmInfo("fagan")?.name).toBe("Fagan inspection");
    expect(algorithmInfo("cocomo_ii")?.name).toBe("COCOMO II");
  });

  it("each match carries a non-empty description", () => {
    for (const a of ["UCP", "SCP", "COCOMO_II", "Fagan", "CMP", "TPA_Plan_A"]) {
      expect(algorithmInfo(a)?.description.length).toBeGreaterThan(20);
    }
  });

  it("returns null for unknown or missing algorithms", () => {
    expect(algorithmInfo("MYSTERY")).toBeNull();
    expect(algorithmInfo("")).toBeNull();
    expect(algorithmInfo(undefined)).toBeNull();
  });
});

describe("algorithmColor", () => {
  it("gives each algorithm a distinct hex color", () => {
    const colors = ["UCP", "SCP", "COCOMO_II", "Fagan", "CMP", "TPA_Plan_A"].map(
      algorithmColor,
    );
    colors.forEach((c) => expect(c).toMatch(/^#[0-9a-f]{6}$/i));
    expect(new Set(colors).size).toBe(6); // all distinct
  });

  it("falls back to a neutral color for unknown algorithms", () => {
    expect(algorithmColor("MYSTERY")).toBe("#94a3b8");
    expect(algorithmColor(undefined)).toBe("#94a3b8");
  });
});

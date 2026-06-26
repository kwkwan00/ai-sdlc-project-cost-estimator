import { describe, expect, it } from "vitest";

import type { CustomRoleInput } from "./schemas";
import { designateLabels, designateTeamMembers, memberDesignation } from "./team-roster";

function role(overrides: Partial<CustomRoleInput> & { role_id: string; description: string }): CustomRoleInput {
  return {
    category: "engineering",
    seniority: "mid",
    rate_per_hour: 100,
    percentage: 0,
    ...overrides,
  };
}

describe("memberDesignation", () => {
  it("maps 0→A, 25→Z (single letters)", () => {
    expect(memberDesignation(0)).toBe("A");
    expect(memberDesignation(1)).toBe("B");
    expect(memberDesignation(25)).toBe("Z");
  });

  it("rolls over to AA/AB past Z (spreadsheet-style)", () => {
    expect(memberDesignation(26)).toBe("AA");
    expect(memberDesignation(27)).toBe("AB");
  });
});

describe("designateTeamMembers", () => {
  it("leaves a unique role un-suffixed with a null designation", () => {
    const out = designateTeamMembers([role({ role_id: "r1", description: "Product Manager" })]);
    expect(out).toHaveLength(1);
    expect(out[0].designation).toBeNull();
    expect(out[0].label).toBe("Product Manager");
  });

  it("suffixes duplicate roles with A/B/C in roster order", () => {
    const out = designateTeamMembers([
      role({ role_id: "r1", description: "Senior Engineer" }),
      role({ role_id: "r2", description: "Senior Engineer" }),
      role({ role_id: "r3", description: "Senior Engineer" }),
    ]);
    expect(out.map((m) => m.label)).toEqual([
      "Senior Engineer A",
      "Senior Engineer B",
      "Senior Engineer C",
    ]);
    expect(out.map((m) => m.designation)).toEqual(["A", "B", "C"]);
  });

  it("disambiguates only the roles that actually repeat", () => {
    const out = designateTeamMembers([
      role({ role_id: "r1", description: "Engineer" }),
      role({ role_id: "r2", description: "Designer" }),
      role({ role_id: "r3", description: "Engineer" }),
    ]);
    expect(out.map((m) => m.label)).toEqual(["Engineer A", "Designer", "Engineer B"]);
    expect(out[1].designation).toBeNull();
  });

  it("preserves the original role fields", () => {
    const [m] = designateTeamMembers([
      role({ role_id: "r1", description: "QA Lead", category: "qa", seniority: "senior", rate_per_hour: 140, percentage: 25 }),
    ]);
    expect(m).toMatchObject({ role_id: "r1", category: "qa", seniority: "senior", rate_per_hour: 140, percentage: 25 });
  });

  it("returns an empty list for an empty roster", () => {
    expect(designateTeamMembers([])).toEqual([]);
  });
});

describe("designateLabels", () => {
  it("suffixes only duplicated descriptions, read via an accessor", () => {
    const items = [
      { k: "1", d: "Engineer" },
      { k: "2", d: "Engineer" },
      { k: "3", d: "PM" },
    ];
    const out = designateLabels(items, (i) => i.d);
    expect(out.map((o) => o.label)).toEqual(["Engineer A", "Engineer B", "PM"]);
    expect(out.map((o) => o.designation)).toEqual(["A", "B", null]);
  });
});

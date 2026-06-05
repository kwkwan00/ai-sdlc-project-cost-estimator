import { describe, expect, it } from "vitest";

import {
  customRoleSchema,
  DEFAULT_ROSTER,
  roleRosterSchema,
  stage1Schema,
  stage2Schema,
  stage3Schema,
} from "./schemas";

describe("stage1Schema", () => {
  it("rejects raw_input shorter than 10 characters", () => {
    const result = stage1Schema.safeParse({ raw_input: "too short" });
    expect(result.success).toBe(false);
  });

  it("accepts a meaningful project description", () => {
    const result = stage1Schema.safeParse({
      raw_input: "Build a patient portal for a clinic with appointments.",
    });
    expect(result.success).toBe(true);
  });
});

describe("customRoleSchema", () => {
  it("defaults category and seniority to 'other' when omitted", () => {
    const r = customRoleSchema.parse({ role_id: "x", description: "Eng" });
    expect(r.category).toBe("other");
    expect(r.seniority).toBe("other");
    expect(r.rate_per_hour).toBe(0);
    expect(r.percentage).toBe(0);
  });

  it("rejects an empty role_id", () => {
    expect(customRoleSchema.safeParse({ role_id: "", description: "x" }).success).toBe(false);
  });

  it("rejects an empty description", () => {
    expect(customRoleSchema.safeParse({ role_id: "x", description: "" }).success).toBe(false);
  });

  it("rejects a description over 500 characters", () => {
    const long = "x".repeat(501);
    expect(customRoleSchema.safeParse({ role_id: "x", description: long }).success).toBe(false);
  });

  it("accepts a 500-character description", () => {
    const long = "x".repeat(500);
    expect(customRoleSchema.safeParse({ role_id: "x", description: long }).success).toBe(true);
  });

  it("rejects an out-of-range percentage", () => {
    expect(
      customRoleSchema.safeParse({ role_id: "x", description: "x", percentage: 120 }).success
    ).toBe(false);
  });
});

describe("roleRosterSchema", () => {
  it("accepts the default roster (4 roles summing to 100)", () => {
    const result = roleRosterSchema.safeParse({ roles: DEFAULT_ROSTER });
    expect(result.success).toBe(true);
  });

  it("rejects duplicate role_ids", () => {
    const result = roleRosterSchema.safeParse({
      roles: [
        { role_id: "a", description: "A", category: "other", seniority: "other", rate_per_hour: 0, percentage: 50 },
        { role_id: "a", description: "B", category: "other", seniority: "other", rate_per_hour: 0, percentage: 50 },
      ],
    });
    expect(result.success).toBe(false);
  });

  it("rejects percentages that do not sum to 100", () => {
    const result = roleRosterSchema.safeParse({
      roles: [
        { role_id: "a", description: "A", category: "other", seniority: "other", rate_per_hour: 0, percentage: 30 },
        { role_id: "b", description: "B", category: "other", seniority: "other", rate_per_hour: 0, percentage: 30 },
      ],
    });
    expect(result.success).toBe(false);
  });

  it("accepts a tagged custom roster of any size", () => {
    const result = roleRosterSchema.safeParse({
      roles: [
        { role_id: "tl", description: "Tech Lead — architecture + reviews", category: "engineering", seniority: "senior", rate_per_hour: 280, percentage: 40 },
        { role_id: "swe", description: "Software engineer building features", category: "engineering", seniority: "mid", rate_per_hour: 180, percentage: 30 },
        { role_id: "qa", description: "QA engineer owning the regression suite", category: "qa", seniority: "mid", rate_per_hour: 160, percentage: 20 },
        { role_id: "devops", description: "DevOps engineer running infra", category: "devops", seniority: "senior", rate_per_hour: 220, percentage: 10 },
      ],
    });
    expect(result.success).toBe(true);
  });

  it("tolerates 0.5 percentage-point rounding drift", () => {
    const result = roleRosterSchema.safeParse({
      roles: [
        { role_id: "a", description: "A", category: "other", seniority: "other", rate_per_hour: 0, percentage: 49.5 },
        { role_id: "b", description: "B", category: "other", seniority: "other", rate_per_hour: 0, percentage: 50.5 },
      ],
    });
    expect(result.success).toBe(true);
  });

  it("accepts an empty roles list (twins will fall back to defaults)", () => {
    expect(roleRosterSchema.safeParse({ roles: [] }).success).toBe(true);
  });
});

describe("DEFAULT_ROSTER", () => {
  it("provides non-empty descriptions on every default role", () => {
    for (const role of DEFAULT_ROSTER) {
      expect(role.description.trim()).not.toBe("");
    }
  });
});

describe("stage2Schema", () => {
  it("defaults the roster to DEFAULT_ROSTER when omitted", () => {
    const result = stage2Schema.parse({});
    expect(result.roster.roles.length).toBe(DEFAULT_ROSTER.length);
    expect(result.roster.roles[0].role_id).toBe(DEFAULT_ROSTER[0].role_id);
  });
});

describe("stage3Schema", () => {
  it("defaults all maturity levels to 1", () => {
    const result = stage3Schema.parse({});
    expect(result.discovery_maturity).toBe(1);
    expect(result.ux_design_maturity).toBe(1);
    expect(result.qa_testing_maturity).toBe(1);
  });

  it("rejects maturity levels outside 1..5", () => {
    expect(stage3Schema.safeParse({ discovery_maturity: 0 }).success).toBe(false);
    expect(stage3Schema.safeParse({ discovery_maturity: 6 }).success).toBe(false);
    expect(stage3Schema.safeParse({ discovery_maturity: 3 }).success).toBe(true);
  });
});

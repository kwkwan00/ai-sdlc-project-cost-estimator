import { describe, expect, it } from "vitest";

import {
  classifyToolingResponseSchema,
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
  it("defaults codebase_context, a blank tooling description, and per-phase ai_tooling to 'none'", () => {
    const result = stage3Schema.parse({});
    expect(result.codebase_context).toBe("greenfield");
    expect(result.ai_tooling_description).toBe("");
    expect(result.ai_tooling).toEqual({
      discovery: "none",
      ux_design: "none",
      development: "none",
      code_review: "none",
      deployment: "none",
      qa_testing: "none",
    });
  });

  it("keeps the freeform tooling description the user typed", () => {
    const result = stage3Schema.parse({
      ai_tooling_description: "Claude Code for dev, CodeRabbit on PRs",
    });
    expect(result.ai_tooling_description).toBe(
      "Claude Code for dev, CodeRabbit on PRs"
    );
    // Description alone does not set any per-phase level — classification fills those.
    expect(result.ai_tooling.development).toBe("none");
  });

  it("applies a per-phase tooling value while defaulting the rest to 'none'", () => {
    const result = stage3Schema.parse({ ai_tooling: { development: "agentic" } });
    expect(result.ai_tooling.development).toBe("agentic");
    expect(result.ai_tooling.discovery).toBe("none");
    expect(result.ai_tooling.ux_design).toBe("none");
    expect(result.ai_tooling.code_review).toBe("none");
    expect(result.ai_tooling.deployment).toBe("none");
    expect(result.ai_tooling.qa_testing).toBe("none");
  });

  it("rejects an invalid enum value", () => {
    expect(stage3Schema.safeParse({ codebase_context: "nope" }).success).toBe(false);
    expect(
      stage3Schema.safeParse({ ai_tooling: { development: "bogus" } }).success
    ).toBe(false);
  });

  it("accepts valid enum values", () => {
    const result = stage3Schema.safeParse({
      codebase_context: "brownfield_large_familiar",
      ai_tooling: { development: "agentic", qa_testing: "chat" },
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.codebase_context).toBe("brownfield_large_familiar");
      expect(result.data.ai_tooling.development).toBe("agentic");
      expect(result.data.ai_tooling.qa_testing).toBe("chat");
      expect(result.data.ai_tooling.discovery).toBe("none");
    }
  });
});

describe("classifyToolingResponseSchema", () => {
  it("parses the classify-tooling endpoint response", () => {
    const result = classifyToolingResponseSchema.parse({
      ai_tooling: { development: "agentic", code_review: "agentic" },
      unknown_tools: ["ZebraAI"],
      notes: "Claude Code → dev+review",
    });
    expect(result.ai_tooling.development).toBe("agentic");
    expect(result.ai_tooling.discovery).toBe("none"); // unspecified phases default
    expect(result.unknown_tools).toEqual(["ZebraAI"]);
  });

  it("defaults unknown_tools and notes when omitted", () => {
    const result = classifyToolingResponseSchema.parse({
      ai_tooling: { development: "chat" },
    });
    expect(result.unknown_tools).toEqual([]);
    expect(result.notes).toBe("");
  });
});

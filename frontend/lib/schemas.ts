import { z } from "zod";

/** Mirrors backend models/project_schema.py + models/twin_outputs.py — keep in sync. */

export const projectTypeEnum = z.enum([
  "greenfield",
  "legacy_replacement",
  "enhancement",
  "integration",
  "data_migration",
  "ai_ml_build",
]);

export const engagementModelEnum = z.enum([
  "fixed_price",
  "tm",
  "retainer",
  "hybrid",
]);

export const roleCategoryEnum = z.enum([
  "product",
  "engineering",
  "ui_ux",
  "qa",
  "devops",
  "data",
  "other",
]);

export const roleSeniorityEnum = z.enum(["senior", "mid", "junior", "other"]);

export type RoleCategory = z.infer<typeof roleCategoryEnum>;
export type RoleSeniority = z.infer<typeof roleSeniorityEnum>;

export const customRoleSchema = z.object({
  role_id: z.string().min(1).max(64),
  description: z.string().min(1).max(500),
  category: roleCategoryEnum.default("other"),
  seniority: roleSeniorityEnum.default("other"),
  rate_per_hour: z.number().min(0).default(0),
  percentage: z.number().min(0).max(100).default(0),
});

export type CustomRoleInput = z.infer<typeof customRoleSchema>;

export const roleRosterSchema = z
  .object({
    roles: z.array(customRoleSchema).default([]),
  })
  .refine(
    (v) => {
      if (v.roles.length === 0) return true;
      const ids = new Set(v.roles.map((r) => r.role_id));
      if (ids.size !== v.roles.length) return false;
      const total = v.roles.reduce((a, r) => a + r.percentage, 0);
      return Math.abs(total - 100) <= 0.5;
    },
    {
      message:
        "Role roster must have unique role_ids and percentages summing to 100",
    }
  );

export type RoleRosterInput = z.infer<typeof roleRosterSchema>;

/** Default roster — kept in sync with backend's RoleRoster.default(). */
export const DEFAULT_ROSTER: CustomRoleInput[] = [
  {
    role_id: "sr_product",
    description: "Senior product manager",
    category: "product",
    seniority: "senior",
    rate_per_hour: 220,
    percentage: 20,
  },
  {
    role_id: "jr_product",
    description: "Junior product manager",
    category: "product",
    seniority: "junior",
    rate_per_hour: 140,
    percentage: 10,
  },
  {
    role_id: "sr_engineer",
    description: "Senior software engineer",
    category: "engineering",
    seniority: "senior",
    rate_per_hour: 240,
    percentage: 50,
  },
  {
    role_id: "jr_engineer",
    description: "Junior software engineer",
    category: "engineering",
    seniority: "junior",
    rate_per_hour: 150,
    percentage: 20,
  },
];

export const stage2Schema = z.object({
  industry: z.string().default(""),
  project_type: projectTypeEnum.default("greenfield"),
  screen_count_estimate: z.number().int().min(0).optional(),
  integration_count: z.number().int().min(0).default(0),
  integration_list: z.array(z.string()).default([]),
  engagement_model: engagementModelEnum.default("tm"),
  target_timeline_weeks: z.number().int().min(1).optional(),
  regulatory_requirements: z.array(z.string()).default([]),
  roster: roleRosterSchema.default({ roles: DEFAULT_ROSTER }),
});

export const stage3Schema = z.object({
  discovery_maturity: z.number().int().min(1).max(5).default(1),
  ux_design_maturity: z.number().int().min(1).max(5).default(1),
  development_maturity: z.number().int().min(1).max(5).default(1),
  code_review_maturity: z.number().int().min(1).max(5).default(1),
  deployment_maturity: z.number().int().min(1).max(5).default(1),
  qa_testing_maturity: z.number().int().min(1).max(5).default(1),
});

export const stage1Schema = z.object({
  project_name: z.string().optional(),
  raw_input: z
    .string()
    .min(10, "Please describe the project in at least a sentence or two."),
});

export const createEstimateSchema = z.object({
  project_name: z.string().optional(),
  raw_input: z.string().min(10),
  stage2: stage2Schema.optional(),
  stage3: stage3Schema.optional(),
});

export type Stage1Input = z.infer<typeof stage1Schema>;
export type Stage2Input = z.infer<typeof stage2Schema>;
export type Stage3Input = z.infer<typeof stage3Schema>;
export type CreateEstimateInput = z.infer<typeof createEstimateSchema>;

export const REGULATORY_OPTIONS = [
  "HIPAA",
  "SOC 2",
  "PCI-DSS",
  "GDPR",
  "FedRAMP",
  "FERPA",
] as const;

export const INDUSTRY_OPTIONS = [
  "healthcare",
  "fintech",
  "insurance",
  "retail",
  "manufacturing",
  "government",
  "education",
  "media",
  "telecom",
  "other",
] as const;

export const ROLE_CATEGORY_LABELS: Record<RoleCategory, string> = {
  product: "Product",
  engineering: "Engineering",
  ui_ux: "UI / UX",
  qa: "QA / Testing",
  devops: "DevOps",
  data: "Data",
  other: "Other",
};

export const ROLE_SENIORITY_LABELS: Record<RoleSeniority, string> = {
  senior: "Senior",
  mid: "Mid",
  junior: "Junior",
  other: "Other",
};

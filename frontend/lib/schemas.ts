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

export const codebaseContextSchema = z.enum([
  "greenfield",
  "brownfield_small",
  "brownfield_large_unfamiliar",
  "brownfield_large_familiar",
]);

export const aiToolingLevelSchema = z.enum([
  "none",
  "autocomplete",
  "chat",
  "agentic",
]);

export const phaseToolingSchema = z.object({
  discovery: aiToolingLevelSchema.default("none"),
  ux_design: aiToolingLevelSchema.default("none"),
  development: aiToolingLevelSchema.default("none"),
  code_review: aiToolingLevelSchema.default("none"),
  deployment: aiToolingLevelSchema.default("none"),
  qa_testing: aiToolingLevelSchema.default("none"),
});

/** All-"none" per-phase tooling. Exported so callers can reuse it as a value. */
export const NO_TOOLING: z.infer<typeof phaseToolingSchema> = {
  discovery: "none",
  ux_design: "none",
  development: "none",
  code_review: "none",
  deployment: "none",
  qa_testing: "none",
};

export const stage3Schema = z.object({
  codebase_context: codebaseContextSchema.default("greenfield"),
  // The user describes their AI tooling in free text; a backend agent classifies
  // it into per-phase levels on submit. `ai_tooling` holds that classified result
  // (all "none" until classified, so a blank description never inflates the estimate).
  ai_tooling_description: z.string().max(2000).default(""),
  ai_tooling: phaseToolingSchema.default({ ...NO_TOOLING }),
  // Technologies the client already uses or proposes (languages, frameworks, cloud,
  // datastores). An estimation signal the twins read, and the one place the user names
  // their stack so the estimate may reference those specific technologies.
  technology_stack: z.string().max(2000).default(""),
});

// Response of POST /estimates/draft/classify-tooling.
export const classifyToolingResponseSchema = z.object({
  ai_tooling: phaseToolingSchema,
  unknown_tools: z.array(z.string()).default([]),
  notes: z.string().default(""),
});

export type CodebaseContext = z.infer<typeof codebaseContextSchema>;
export type AiToolingLevel = z.infer<typeof aiToolingLevelSchema>;
export type PhaseTooling = z.infer<typeof phaseToolingSchema>;
export type ClassifyToolingResponse = z.infer<typeof classifyToolingResponseSchema>;

export const stage1Schema = z.object({
  project_name: z.string().optional(),
  raw_input: z
    .string()
    .min(10, "Please describe the project in at least a sentence or two."),
});

// The six SDLC phases a user may choose to estimate (matches the backend `Phase` enum and the
// `Phase` union in lib/types.ts). Omitted from a request ⇒ the backend estimates all six.
export const phaseEnum = z.enum([
  "discovery",
  "ux_design",
  "development",
  "code_review",
  "deployment",
  "qa_testing",
]);

// Canonical source for the SDLC phase identifiers. `lib/types.ts` re-exports this `Phase` (the
// same schema-as-source-of-truth pattern as RoleCategory/RoleSeniority), so request validation
// (selected_phases) and every UI consumer stay in lock-step — adding a phase here updates both.
export type Phase = z.infer<typeof phaseEnum>;

export const createEstimateSchema = z.object({
  project_name: z.string().optional(),
  raw_input: z.string().min(10),
  stage2: stage2Schema.optional(),
  stage3: stage3Schema.optional(),
  // Subset of phases to estimate; omitted ⇒ all six. The wizard sends `undefined` when the user
  // leaves every phase selected, so existing estimate requests stay byte-identical.
  selected_phases: z.array(phaseEnum).optional(),
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

// Canonical `{value,label}` option lists derived from the label maps — the single source of truth
// for every category/seniority <select> (roster editor + Settings rate card). Don't re-derive these
// per component, or the dropdowns can silently diverge.
export const ROLE_CATEGORY_OPTIONS = (
  Object.entries(ROLE_CATEGORY_LABELS) as [RoleCategory, string][]
).map(([value, label]) => ({ value, label }));
export const ROLE_SENIORITY_OPTIONS = (
  Object.entries(ROLE_SENIORITY_LABELS) as [RoleSeniority, string][]
).map(([value, label]) => ({ value, label }));

export const CODEBASE_CONTEXT_LABELS: Record<CodebaseContext, string> = {
  greenfield: "Greenfield (new codebase)",
  brownfield_small: "Brownfield — small / modular",
  brownfield_large_unfamiliar: "Brownfield — large, unfamiliar to the team",
  brownfield_large_familiar: "Brownfield — large, well-known to the team",
};

export const AI_TOOLING_LEVEL_LABELS: Record<AiToolingLevel, string> = {
  none: "None (no AI tooling)",
  autocomplete: "Inline autocomplete (Copilot-style)",
  chat: "AI chat alongside coding",
  agentic: "Agentic coding (Cursor / Claude Code)",
};

export type PhaseToolingKey = keyof z.infer<typeof phaseToolingSchema>;

export const PHASE_TOOLING_META: {
  key: PhaseToolingKey;
  label: string;
  examples: string;
}[] = [
  {
    key: "discovery",
    label: "Discovery / Requirements",
    examples: "Claude, Claude Cowork (research, summarization)",
  },
  {
    key: "ux_design",
    label: "UX / Design",
    examples: "Figma AI, Claude Cowork, v0",
  },
  {
    key: "development",
    label: "Development",
    examples: "Claude Code, Cursor, GitHub Copilot",
  },
  {
    key: "code_review",
    label: "Code Review",
    examples: "Claude Code, CodeRabbit, Greptile",
  },
  {
    key: "deployment",
    label: "Deployment / DevOps",
    examples: "Harness.io, AI CI/CD copilots",
  },
  {
    key: "qa_testing",
    label: "QA / Testing",
    examples: "LangSmith, AI test generation",
  },
];

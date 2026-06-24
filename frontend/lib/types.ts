/** Response types mirroring backend pydantic models. Wire format only — not validated. */

import type { Phase, RoleCategory, RoleSeniority } from "./schemas";
import type { WbsTaskInput } from "./wbs";

export type EstimateStatus =
  | "pending"
  | "pass_1_running"
  | "awaiting_answers"
  | "pass_2_running"
  | "synthesizing"
  | "completed"
  | "failed";

// Phase is defined once in lib/schemas.ts (the zod `phaseEnum`) and re-exported here so the many
// `@/lib/types` consumers keep working and can't drift from request validation.
export type { Phase };

export interface HourRange {
  optimistic: number;
  most_likely: number;
  pessimistic: number;
  /** Monte Carlo dispersion (std-dev of the sampled distribution), when available. */
  std?: number;
  /** Mean of the sampled distribution, when available. */
  mean?: number;
  /** Sampled percentiles keyed "p5".."p95", when the estimate was simulated. */
  percentiles?: Record<string, number>;
}

export interface RoleHours {
  role_id: string;
  role_description: string;
  category: RoleCategory;
  seniority: RoleSeniority;
  hours: number;
}

export interface RoleHeadcount {
  role_id: string;
  role_description: string;
  category: RoleCategory;
  seniority: RoleSeniority;
  headcount: number;
  rate_per_hour: number;
  ai_assisted_hours: number;
  manual_only_hours: number;
  ai_assisted_cost_usd: number;
  manual_only_cost_usd: number;
}

export interface LlmModelUsage {
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd: number;
}

export interface LlmUsage {
  call_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd: number;
  by_model: LlmModelUsage[];
}

export interface Assumption {
  text: string;
  impact_hours: number;
}

export interface Risk {
  description: string;
  likelihood: number;
  impact_hours_low: number;
  impact_hours_high: number;
}

export interface ClarifyingQuestion {
  id: string;
  text: string;
  source_phases: Phase[];
  suggested_default: string;
  impact_hours: number;
  answered: boolean;
  answer: string | null;
}

export interface PhaseEstimate {
  phase: Phase;
  twin_name: string;
  algorithm: string;
  ai_assisted_hours: HourRange;
  manual_only_hours: HourRange;
  ai_assisted_role_hours: RoleHours[];
  manual_only_role_hours: RoleHours[];
  assumptions: Assumption[];
  risks: Risk[];
  confidence: number;
  breakdown: Record<string, number>;
  effective_ai_reduction_pct: number;
  notes: string;
}

export interface DualScenarioEstimate {
  total_ai_assisted_hours: HourRange;
  total_manual_only_hours: HourRange;
  ai_hours_saved_pert: number;
  ai_cost_saved_usd: number;
  phases: PhaseEstimate[];
  confidence: number;
  duration_weeks_low: number;
  duration_weeks_high: number;
  headcount_by_role: RoleHeadcount[];
  weekly_burn_rate_usd: number;
  // Team-scaling (Brooks's Law + diminishing returns) outputs — optional so persisted
  // pre-feature estimates still deserialize.
  brooks_overhead_pct?: number;
  staffing_efficiency_pct?: number;
  team_size?: number;
  optimal_team_size?: number;
  // Contingency management-reserve % applied to cost + timeline (0/absent = none).
  contingency_pct?: number;
  total_cost_ai_assisted_usd: number;
  total_cost_manual_only_usd: number;
  llm_usage: LlmUsage;
}

export interface EstimateEnvelope {
  estimate_id: string;
  project_name: string;
  status: EstimateStatus;
  created_at: string;
  pass1_estimates: PhaseEstimate[];
  clarifying_questions: ClarifyingQuestion[];
  pass2_estimates: PhaseEstimate[];
  final_estimate: DualScenarioEstimate | null;
  error: string | null;
  /** Which flow produced this: "twins" (default) or "wbs" (bottom-up). Defaulted server-side. */
  method?: "twins" | "wbs";
  /** WBS-only: the finalized task tree, for the review-page tree panel + Duplicate. */
  wbs_tree?: WbsTaskInput[] | null;
}

// --- Statement of Work (SOW) export -------------------------------------------------
export type SowScenario = "ai_assisted" | "manual_only";
export type SowSectionKind =
  | "paragraph"
  | "bullets"
  | "table"
  | "signature_block"
  | "cover";

export interface SowTable {
  columns: string[];
  rows: string[][];
}

export interface SowSignatory {
  party: string;
  fields: string[];
}

export interface SowSectionContent {
  id: string;
  heading: string;
  kind: SowSectionKind;
  text: string;
  bullets: string[];
  table: SowTable | null;
  signatories: SowSignatory[];
  /** UI hint: prose/bullets are editable; tables/cover/signature blocks are read-only. */
  editable: boolean;
}

export interface SowDocument {
  estimate_id: string;
  template_id: string;
  title: string;
  project_name: string;
  scenario: SowScenario;
  sections: SowSectionContent[];
  /** Bracketed tokens still unresolved (e.g. "[CLIENT NAME]") — the user fills these in Word. */
  placeholders: string[];
}

export interface SowGenerateResponse {
  document: SowDocument;
  /** Token cost of generating this SOW (separate from the estimate's own llm_usage). */
  llm_usage: LlmUsage;
}

export const PHASE_LABELS: Record<Phase, string> = {
  discovery: "Discovery",
  ux_design: "UX / Design",
  development: "Development",
  code_review: "Code Review",
  deployment: "Deployment / DevOps",
  qa_testing: "QA / Testing",
};

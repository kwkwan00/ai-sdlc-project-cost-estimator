/** Content/metadata for the Settings screen — label + hint copy for the admin-switchable
 *  sizing methods and the team-scaling coefficients. Kept out of the page component (it's
 *  reference content, like lib/algorithms.ts), so the page holds logic, not copy. */

export interface MethodLabel {
  label: string;
  hint: string;
}

export const DISCOVERY_SIZING_LABELS: Record<string, MethodLabel> = {
  ucp: {
    label: "Use Case Points (UCP)",
    hint: "Sizes discovery off classified use cases + actors × technical/environmental factors — the calibrated default.",
  },
  function_points: {
    label: "FP-based analysis effort",
    hint: "Scales discovery/analysis hours linearly off the project's function-point count — better for FP-anchored scopes.",
  },
};

export const DEV_SIZING_LABELS: Record<string, MethodLabel> = {
  cocomo: {
    label: "COCOMO II",
    hint: "Effort scales super-linearly with code size (KSLOC^E) — the calibrated default.",
  },
  function_points: {
    label: "Function Points (IFPUG)",
    hint: "Effort scales linearly with function points (FP × hours/FP) — better for feature-counted scopes.",
  },
  cosmic_function_points: {
    label: "COSMIC Function Points (ISO 19761)",
    hint: "Effort scales linearly with COSMIC functional size (data movements) — better for real-time, embedded, and service-oriented systems.",
  },
};

export const QA_SIZING_LABELS: Record<string, MethodLabel> = {
  tpa: {
    label: "Test Point Analysis (TPA)",
    hint: "Sizes testing off function points × dynamic/static quality characteristics — the calibrated default.",
  },
  test_case_point: {
    label: "Test Case Point Analysis (TCPA)",
    hint: "Sizes testing off the planned test-case count weighted by complexity — better when you count test cases.",
  },
  defect_removal: {
    label: "Defect Removal (Capers-Jones)",
    hint: "Sizes testing off the defects a project of this size will contain (defect potential × removal effort) — quality-driven rather than count-driven.",
  },
};

export interface CoefficientMeta {
  label: string;
  hint: string;
  step: number;
}

export const COEFF_META: Record<string, CoefficientMeta> = {
  link_cost: {
    label: "Coordination tax per link",
    hint: "Capacity fraction lost per communication link (Brooks's Law).",
    step: 0.01,
  },
  free_team_size: {
    label: "Coordination-free team size",
    hint: "No coordination overhead at or below this team size.",
    step: 1,
  },
  overhead_cap: {
    label: "Max coordination overhead",
    hint: "Cap on the overhead applied to cost + schedule.",
    step: 0.05,
  },
  diminishing_returns_exponent: {
    label: "Diminishing-returns exponent (β)",
    hint: "n^β throughput — 1.0 = perfectly parallel; lower = stronger diminishing returns.",
    step: 0.01,
  },
};

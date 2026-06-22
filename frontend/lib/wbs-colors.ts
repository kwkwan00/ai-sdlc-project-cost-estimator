/** Per-phase colors shared by the WBS tree views (row swatches, legend, modal badges). */

import type { Phase } from "./types";

export const PHASE_COLORS: Record<Phase, string> = {
  discovery: "#6366f1",
  ux_design: "#ec4899",
  development: "#3b82f6",
  code_review: "#f59e0b",
  deployment: "#10b981",
  qa_testing: "#8b5cf6",
};

export const PHASE_FALLBACK_COLOR = "#94a3b8";

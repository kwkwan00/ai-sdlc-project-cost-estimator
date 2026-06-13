/** Human-readable descriptions of the six estimation algorithms the twins use,
 *  surfaced as tooltips on the review page. Matched by substring against a phase's
 *  `algorithm` string (e.g. "COCOMO_II" → COCOMO, "TPA_Plan_B" → TPA). */

export interface AlgorithmInfo {
  /** Substring matched against the phase `algorithm` value. */
  abbr: string;
  name: string;
  description: string;
  /** Distinct color for charts/badges so each algorithm reads at a glance. */
  color: string;
}

const FALLBACK_COLOR = "#94a3b8"; // slate-400, for unknown algorithms

// Order matters only for readability; matching is by substring, and the abbrs are
// distinct enough not to collide.
const ALGORITHMS: AlgorithmInfo[] = [
  {
    abbr: "UCP",
    name: "Use Case Points (UCP)",
    color: "#6366f1", // indigo
    description:
      "Sizes discovery/requirements effort by counting and weighting use cases and actors, then adjusting for 13 technical and 8 environmental complexity factors.",
  },
  {
    abbr: "SCP",
    name: "Screen Complexity Points (SCP)",
    color: "#ec4899", // pink
    description:
      "Sizes UX/design effort by scoring each screen's complexity (simple → novel) and multiplying by design-system, interaction-complexity, and iteration factors.",
  },
  {
    abbr: "COCOMO",
    name: "COCOMO II",
    color: "#0ea5e9", // sky
    description:
      "Parametric software-cost model: derives build effort from size (function points or SLOC) raised to a scale exponent, adjusted by effort-multiplier cost drivers and a tech-stack factor.",
  },
  {
    abbr: "FAGAN",
    name: "Fagan inspection",
    color: "#f59e0b", // amber
    description:
      "Estimates code-review effort from a formal inspection rate (lines reviewed per hour) plus preparation and rework, scaled by PR complexity and kickback rate.",
  },
  {
    abbr: "CMP",
    name: "Cloud Migration Points (CMP)",
    color: "#10b981", // emerald
    description:
      "Sizes deployment/DevOps effort from infrastructure complexity, the count of CI/CD and monitoring components, a regulatory multiplier, and a conservative bias.",
  },
  {
    abbr: "TPA",
    name: "Test Point Analysis (TPA)",
    color: "#8b5cf6", // violet
    description:
      "Sizes QA/testing effort from function points weighted by dynamic and static quality characteristics, for the chosen test strategy (eval harness, QA team, or hybrid).",
  },
];

export function algorithmInfo(algorithm: string | undefined): AlgorithmInfo | null {
  if (!algorithm) return null;
  const key = algorithm.toUpperCase();
  return ALGORITHMS.find((a) => key.includes(a.abbr)) ?? null;
}

/** The algorithm's chart/badge color, or a neutral slate for unknowns. */
export function algorithmColor(algorithm: string | undefined): string {
  return algorithmInfo(algorithm)?.color ?? FALLBACK_COLOR;
}

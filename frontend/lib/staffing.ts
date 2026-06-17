import type { DualScenarioEstimate } from "./types";

/** Derived team-scaling readout for the review page's staffing section. Pure formatting +
 *  over/under-staffing classification over the backend's Brooks/diminishing-returns fields. */
export interface StaffingSummary {
  /** False when the team-scaling fields are absent (legacy/persisted estimate) — render nothing. */
  present: boolean;
  overheadPct: number;
  efficiencyPct: number;
  teamSize: number;
  optimalTeamSize: number;
  /** Team size relative to the Brooks/diminishing-returns sweet spot. */
  staffing: "overstaffed" | "understaffed" | "balanced";
  /** Compact label, e.g. "+21% coordination overhead · 78% scaling efficiency · sweet spot ≈ 6". */
  label: string;
}

export function staffingSummary(fe: DualScenarioEstimate): StaffingSummary {
  const teamSize = fe.team_size ?? 0;
  const optimalTeamSize = fe.optimal_team_size ?? 0;
  const overheadPct = fe.brooks_overhead_pct ?? 0;
  const efficiencyPct = fe.staffing_efficiency_pct ?? 0;
  // Only meaningful once we have a real team size + sweet spot to compare.
  const present = teamSize > 0 && optimalTeamSize > 0;

  let staffing: StaffingSummary["staffing"] = "balanced";
  if (present) {
    // A >1-head gap so rounding can't flip the classification.
    if (teamSize > optimalTeamSize + 1) staffing = "overstaffed";
    else if (teamSize < optimalTeamSize - 1) staffing = "understaffed";
  }

  const label =
    `+${overheadPct}% coordination overhead · ` +
    `${Math.round(efficiencyPct)}% scaling efficiency · ` +
    `sweet spot ≈ ${optimalTeamSize}`;

  return { present, overheadPct, efficiencyPct, teamSize, optimalTeamSize, staffing, label };
}

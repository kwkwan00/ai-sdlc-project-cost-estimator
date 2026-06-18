/** Pure schedule derivation for the review page's Timeline tab. Kept here (not in the
 *  components) so the dependency/critical-path/Monte-Carlo math is unit-testable; the
 *  Gantt + PERT rendering lives in `components/GanttChart.tsx` / `components/PertChart.tsx`.
 *
 *  The estimate has no calendar schedule — only per-phase `HourRange`s and a project
 *  `duration_weeks` band. We derive a *presentational* schedule from those:
 *    - each phase's duration is proportional to its hours (constant productivity per phase),
 *    - phases overlap per a fixed SDLC precedence model (start-to-start "begin when the
 *      predecessor is f% elapsed"),
 *    - the whole thing is scaled so its span equals the estimate's reported duration_weeks,
 *      keeping the Timeline consistent with the headline number.
 *  A forward/backward pass yields the critical path + per-phase slack, and — because every
 *  phase carries a Monte-Carlo `HourRange` — sampling the durations through the same network
 *  gives a project-completion distribution (P(finish by week X)) and a per-phase criticality
 *  index (how often each phase lands on the critical path). */

import type { DualScenarioEstimate, HourRange, Phase, PhaseEstimate } from "./types";
import { PHASE_LABELS } from "./types";
import { hasPercentiles, PERCENTILE_KEYS, type PercentileKey } from "./fan-chart";

export type ScenarioMode = "ai_assisted" | "manual_only";

/** One predecessor of a phase, with a capped overlap into the predecessor's tail:
 *  `overlap = min(tailFrac · pred.dur, leadFrac · succ.dur)`, and the successor starts at
 *  `pred.end − overlap`. Capping by BOTH durations guarantees the successor never starts
 *  before the predecessor (overlap ≤ pred.dur) nor finishes before it (overlap ≤ succ.dur) —
 *  realistic for any size ratio (e.g. a 4,000 h dev phase vs a 250 h review). */
interface Dep {
  pred: Phase;
  tailFrac: number; // max fraction of the predecessor's tail that may overlap
  leadFrac: number; // max fraction of the successor that may lead into that tail
}

function overlapOf(dep: Dep, predDur: number, succDur: number): number {
  return Math.min(dep.tailFrac * predDur, dep.leadFrac * succDur);
}

/** Topological order of the six SDLC phases (every phase's predecessors precede it). */
export const PHASE_ORDER: Phase[] = [
  "discovery",
  "ux_design",
  "development",
  "code_review",
  "qa_testing",
  "deployment",
];

/** Realistic-overlapping SDLC precedence: design overlaps the discovery tail, dev overlaps
 *  design, code-review rides development from ~40%, QA overlaps the dev tail, deployment
 *  follows QA (and a finished code-review). Start-to-start fractions encode the overlap. */
const DEPS: Record<Phase, Dep[]> = {
  discovery: [],
  ux_design: [{ pred: "discovery", tailFrac: 0.4, leadFrac: 0.5 }],
  development: [{ pred: "ux_design", tailFrac: 0.5, leadFrac: 0.5 }],
  code_review: [{ pred: "development", tailFrac: 0.6, leadFrac: 0.9 }],
  qa_testing: [{ pred: "development", tailFrac: 0.5, leadFrac: 0.8 }],
  deployment: [
    { pred: "qa_testing", tailFrac: 0.5, leadFrac: 0.8 },
    { pred: "code_review", tailFrac: 0.9, leadFrac: 0.9 },
  ],
};

/** Phases that anchor a headline milestone, with the label shown at the phase's finish. */
const MILESTONE_AT_PHASE_END: Partial<Record<Phase, string>> = {
  discovery: "Discovery complete",
  ux_design: "Design sign-off",
  development: "Development complete",
  qa_testing: "QA sign-off",
  deployment: "Launch",
};

export interface PhaseSchedule {
  phase: Phase;
  label: string;
  startWeek: number;
  endWeek: number;
  durationWeeks: number;
  /** Weeks this phase can slip without delaying the project (0 → on the critical path). */
  slackWeeks: number;
  isCritical: boolean;
  /** Monte-Carlo: % of iterations this phase landed on the critical path (0–100). */
  criticalityPct: number;
  /** This phase's share of total effort (0–1) — drives bar emphasis. */
  effortShare: number;
}

export interface Milestone {
  name: string;
  week: number;
  kind: "kickoff" | "phase" | "launch";
}

export interface ScheduleRisk {
  /** P10/P90 of the project-completion week across the Monte-Carlo draws. */
  p10Weeks: number;
  p90Weeks: number;
  medianWeeks: number;
  /** Probability the project finishes on or before `week` (a monotone CDF). */
  pFinishBy: (week: number) => number;
  /** True when the draws used real per-phase percentiles; false → triangular fallback. */
  simulated: boolean;
  draws: number;
}

export interface ScheduleResult {
  phases: PhaseSchedule[];
  totalWeeks: number;
  /** Critical-path phases in time order. */
  criticalPath: Phase[];
  milestones: Milestone[];
  /** Phase dependency edges (for the PERT network), as predecessor→successor pairs. */
  edges: { from: Phase; to: Phase }[];
  /** Null when there's nothing to simulate (no hours). */
  risk: ScheduleRisk | null;
}

const CRIT_EPS = 1e-9;

function rangeFor(p: PhaseEstimate, mode: ScenarioMode): HourRange {
  return mode === "ai_assisted" ? p.ai_assisted_hours : p.manual_only_hours;
}

/** Deterministic PRNG (mulberry32) so the Monte-Carlo overlay is stable across renders and
 *  reproducible in tests. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Forward pass: earliest start/finish per phase under the SS precedence, plus project end.
 *  Operates on whatever durations are supplied (point or sampled). */
function forwardPass(durations: Record<Phase, number>): {
  es: Record<Phase, number>;
  ef: Record<Phase, number>;
  projectEnd: number;
} {
  const es = {} as Record<Phase, number>;
  const ef = {} as Record<Phase, number>;
  for (const ph of PHASE_ORDER) {
    let start = 0;
    for (const d of DEPS[ph]) {
      const cand = ef[d.pred] - overlapOf(d, durations[d.pred], durations[ph]);
      if (cand > start) start = cand;
    }
    es[ph] = start;
    ef[ph] = start + durations[ph];
  }
  const projectEnd = Math.max(0, ...PHASE_ORDER.map((ph) => ef[ph]));
  return { es, ef, projectEnd };
}

/** Successor map (inverse of DEPS), carrying each edge's dep so overlap is recomputable. */
const SUCCS: Record<Phase, { succ: Phase; dep: Dep }[]> = (() => {
  const m = {} as Record<Phase, { succ: Phase; dep: Dep }[]>;
  for (const p of PHASE_ORDER) m[p] = [];
  for (const ph of PHASE_ORDER) {
    for (const d of DEPS[ph]) m[d.pred].push({ succ: ph, dep: d });
  }
  return m;
})();

/** Backward pass: latest start per phase (bounded by project end and successors' latest
 *  starts under the SS lag), then slack = latest − earliest. Phases with ~0 slack are
 *  critical. Returns the zero-slack set too. */
function slackAndCritical(
  durations: Record<Phase, number>,
  es: Record<Phase, number>,
  projectEnd: number,
): { slack: Record<Phase, number>; critical: Set<Phase> } {
  const ls = {} as Record<Phase, number>;
  for (const ph of [...PHASE_ORDER].reverse()) {
    let latestFinish = projectEnd;
    for (const s of SUCCS[ph]) {
      // succ.ES = ph.EF − overlap ⇒ ph.EF ≤ succ.LS + overlap (so succ still starts by its LS)
      const cand = ls[s.succ] + overlapOf(s.dep, durations[ph], durations[s.succ]);
      if (cand < latestFinish) latestFinish = cand;
    }
    ls[ph] = latestFinish - durations[ph];
  }
  const slack = {} as Record<Phase, number>;
  const critical = new Set<Phase>();
  const tol = CRIT_EPS * Math.max(1, projectEnd);
  for (const ph of PHASE_ORDER) {
    const s = Math.max(0, ls[ph] - es[ph]);
    slack[ph] = s;
    if (s <= tol) critical.add(ph);
  }
  return { slack, critical };
}

/** Sample one duration for a phase: invert its percentile ladder when simulated, else a
 *  triangular(optimistic, most_likely, pessimistic). Returned in the same units as the input
 *  range (scaling is applied by the caller). */
function sampleHours(range: HourRange, rnd: () => number): number {
  if (hasPercentiles(range)) {
    const probs: Record<PercentileKey, number> = {
      p5: 0.05,
      p10: 0.1,
      p25: 0.25,
      p50: 0.5,
      p75: 0.75,
      p90: 0.9,
      p95: 0.95,
    };
    const u = rnd();
    const keys = PERCENTILE_KEYS;
    // Flat-extrapolate outside [p5, p95].
    if (u <= probs.p5) return range.percentiles.p5;
    if (u >= probs.p95) return range.percentiles.p95;
    for (let i = 1; i < keys.length; i++) {
      const lo = keys[i - 1];
      const hi = keys[i];
      if (u <= probs[hi]) {
        const t = (u - probs[lo]) / (probs[hi] - probs[lo]);
        return range.percentiles[lo] + t * (range.percentiles[hi] - range.percentiles[lo]);
      }
    }
    return range.percentiles.p95;
  }
  // Triangular fallback from the three-point range.
  const o = range.optimistic;
  const m = Math.min(Math.max(range.most_likely, o), range.pessimistic);
  const p = range.pessimistic;
  if (p <= o) return m;
  const u = rnd();
  const fc = (m - o) / (p - o);
  return u < fc ? o + Math.sqrt(u * (p - o) * (m - o)) : p - Math.sqrt((1 - u) * (p - o) * (p - m));
}

/** Derive the presentational schedule + critical path + Monte-Carlo schedule risk for one
 *  scenario. `opts` exposes the draw count + seed for deterministic tests. */
export function deriveSchedule(
  fe: DualScenarioEstimate,
  mode: ScenarioMode,
  opts: { draws?: number; seed?: number } = {},
): ScheduleResult {
  const byPhase = new Map<Phase, PhaseEstimate>(fe.phases.map((p) => [p.phase, p]));
  const mlHours = {} as Record<Phase, number>;
  let totalHours = 0;
  for (const ph of PHASE_ORDER) {
    const est = byPhase.get(ph);
    const h = est ? Math.max(0, rangeFor(est, mode).most_likely) : 0;
    mlHours[ph] = h;
    totalHours += h;
  }

  const edges = PHASE_ORDER.flatMap((to) => DEPS[to].map((d) => ({ from: d.pred, to })));
  const nominalWeeks = Math.max(0, (fe.duration_weeks_low + fe.duration_weeks_high) / 2);

  // Point schedule in raw (hours) units, then scale so the span == nominal duration_weeks.
  const { es, ef, projectEnd } = forwardPass(mlHours);
  const scale = projectEnd > 0 && nominalWeeks > 0 ? nominalWeeks / projectEnd : 0;
  const { slack, critical } = slackAndCritical(mlHours, es, projectEnd);

  const phases: PhaseSchedule[] = PHASE_ORDER.map((ph) => ({
    phase: ph,
    label: PHASE_LABELS[ph],
    startWeek: es[ph] * scale,
    endWeek: ef[ph] * scale,
    durationWeeks: mlHours[ph] * scale,
    slackWeeks: slack[ph] * scale,
    isCritical: critical.has(ph),
    criticalityPct: 0,
    effortShare: totalHours > 0 ? mlHours[ph] / totalHours : 0,
  }));

  const totalWeeks = projectEnd * scale;
  const criticalPath = phases
    .filter((p) => p.isCritical && p.durationWeeks > 0)
    .sort((a, b) => a.startWeek - b.startWeek)
    .map((p) => p.phase);

  const milestones: Milestone[] = [{ name: "Kickoff", week: 0, kind: "kickoff" }];
  for (const ph of PHASE_ORDER) {
    const name = MILESTONE_AT_PHASE_END[ph];
    const sched = phases.find((p) => p.phase === ph);
    if (name && sched && sched.durationWeeks > 0) {
      milestones.push({ name, week: sched.endWeek, kind: ph === "deployment" ? "launch" : "phase" });
    }
  }

  const risk = scale > 0 ? monteCarlo(fe, mode, byPhase, scale, opts) : null;
  if (risk) {
    for (const p of phases) p.criticalityPct = risk_criticality(risk, p.phase);
  }

  return { phases, totalWeeks, criticalPath, milestones, edges, risk };
}

// --- Monte-Carlo schedule risk -------------------------------------------------------------

interface McInternal extends ScheduleRisk {
  criticality: Record<Phase, number>; // 0..100
}

function risk_criticality(risk: ScheduleRisk, phase: Phase): number {
  return (risk as McInternal).criticality[phase] ?? 0;
}

function monteCarlo(
  fe: DualScenarioEstimate,
  mode: ScenarioMode,
  byPhase: Map<Phase, PhaseEstimate>,
  scale: number,
  opts: { draws?: number; seed?: number },
): McInternal {
  const draws = opts.draws ?? 1000;
  const seed = opts.seed ?? Math.max(1, Math.round(fe.total_ai_assisted_hours.most_likely) || 1);
  const rnd = mulberry32(seed);

  const anySim = PHASE_ORDER.some((ph) => {
    const est = byPhase.get(ph);
    return est ? hasPercentiles(rangeFor(est, mode)) : false;
  });

  const counts = Object.fromEntries(PHASE_ORDER.map((p) => [p, 0])) as Record<Phase, number>;
  const ends: number[] = [];

  for (let i = 0; i < draws; i++) {
    const dur = {} as Record<Phase, number>;
    for (const ph of PHASE_ORDER) {
      const est = byPhase.get(ph);
      dur[ph] = est ? Math.max(0, sampleHours(rangeFor(est, mode), rnd)) * scale : 0;
    }
    const { es, projectEnd } = forwardPass(dur);
    ends.push(projectEnd);
    const { critical } = slackAndCritical(dur, es, projectEnd);
    for (const ph of critical) if (dur[ph] > 0) counts[ph] += 1;
  }

  ends.sort((a, b) => a - b);
  const q = (frac: number) => ends[Math.min(ends.length - 1, Math.floor(frac * ends.length))];
  const criticality = Object.fromEntries(
    PHASE_ORDER.map((p) => [p, (counts[p] / draws) * 100]),
  ) as Record<Phase, number>;

  const pFinishBy = (week: number): number => {
    let lo = 0;
    let hi = ends.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (ends[mid] <= week) lo = mid + 1;
      else hi = mid;
    }
    return lo / ends.length;
  };

  return {
    p10Weeks: q(0.1),
    p90Weeks: q(0.9),
    medianWeeks: q(0.5),
    pFinishBy,
    simulated: anySim,
    draws,
    criticality,
  };
}

/** Resource-constrained schedule derivation for the WBS review page's Timeline tab.
 *
 *  The phase-based `lib/schedule.ts` arranges the six fixed SDLC phases by a hard-coded precedence
 *  model. A WBS draft carries a richer structure — per-task `depends_on` edges + a roster `role_id`
 *  per leaf — so here we schedule the actual tasks:
 *    - tasks start after their dependency predecessors finish (`depends_on`, leaf→leaf AND
 *      package→package, the latter expanded so every successor-package leaf waits on every
 *      predecessor-package leaf),
 *    - each team member (`role_id`) is a resource that runs `headcount` tasks at once (so a role
 *      with 3 people parallelizes 3 tasks); tasks competing for the same member serialize,
 *    - everything else runs in parallel.
 *  A greedy serial-schedule-generation pass (list scheduling) places each task at the earliest time
 *  allowed by BOTH its dependencies and its assigned member's next free slot. The makespan is then
 *  scaled to the estimate's reported `duration_weeks` so the Timeline stays consistent with the
 *  headline number. A resource-aware critical chain (follow each task's binding predecessor back
 *  from the last-finishing task) and a transitively-reduced edge set (clean PERT) fall out of it.
 *
 *  Pure + unit-tested; the Gantt/PERT rendering lives in the matching components.
 */

import type { Phase, RoleHeadcount } from "./types";
import { designateLabels } from "./team-roster";
import { effectiveLeafDeps, isLeaf, pertMean, type WbsTaskInput } from "./wbs";

const WORK_HOURS_PER_WEEK = 40;
// A role's parallel-lane count is capped so a pathological headcount can't spawn thousands of
// scheduler slots / Gantt lanes (and can't blow the arg limit of any `Math.min(...slots)` spread).
const MAX_PARALLEL_SLOTS = 50;

export interface WbsScheduledTask {
  id: string;
  name: string;
  phase: Phase | null;
  roleId: string;
  memberLabel: string;
  hours: number;
  startWeek: number;
  endWeek: number;
  durationWeeks: number;
  /** Which of the member's parallel slots ran this task (0-based). */
  slot: number;
  /** Resolved dependency predecessors (task ids) — the full set used for scheduling. */
  deps: string[];
  /** Longest-path depth over the dependency graph (PERT column). */
  rank: number;
  isCritical: boolean;
}

/** One Gantt row: a single member slot (a "lane"). A member with capacity > 1 yields one row per
 *  used slot; tasks within a row never overlap (a slot is a single resource). */
export interface WbsGanttRow {
  roleId: string;
  memberLabel: string;
  slot: number;
  /** True on the first row of a member, so the renderer can group/label slots. */
  firstOfMember: boolean;
  tasks: WbsScheduledTask[];
}

export interface WbsScheduleResult {
  tasks: WbsScheduledTask[];
  /** Member-swimlane rows for the Gantt. */
  rows: WbsGanttRow[];
  /** Transitively-reduced dependency edges (predecessor → successor task ids) for the PERT network. */
  edges: { from: string; to: string }[];
  /** Critical-chain task ids in time order. */
  criticalPath: string[];
  totalWeeks: number;
}

interface RawLeaf {
  id: string;
  name: string;
  phase: Phase | null;
  roleId: string;
  hours: number;
  index: number;
}

/** Collect leaf data (id / name / phase / role / hours) in document order. The dependency graph is
 *  built separately by `effectiveLeafDeps` (shared with the editor's cycle check), so this only
 *  carries what the scheduler needs to place each task. */
function collect(tree: WbsTaskInput[]): RawLeaf[] {
  const leaves: RawLeaf[] = [];
  let index = 0;
  const walk = (nodes: WbsTaskInput[]) => {
    for (const n of nodes) {
      if (isLeaf(n)) {
        leaves.push({
          id: n.id,
          name: n.name,
          phase: n.phase ?? null,
          roleId: (n.role_id ?? "").trim(),
          hours: Math.max(0, pertMean(n.optimistic ?? 0, n.most_likely ?? 0, n.pessimistic ?? 0)),
          index: index++,
        });
      } else {
        walk(n.children);
      }
    }
  };
  walk(tree);
  return leaves;
}

/** Break any dependency cycle into a DAG by dropping back-edges (an edge to a node still on the
 *  current DFS path) so the greedy scheduler always reaches indeg 0. Cycles shouldn't arrive here —
 *  the editor's `dependencyTargets` and the backend sanitize forbid them — but a legacy/hand-edited
 *  tree could carry one, and without this the scheduler would force-place a blocked task before its
 *  predecessors (a schedule that violates the depends_on ordering it claims to honor). */
function breakCycles(ids: string[], deps: Map<string, string[]>): Map<string, string[]> {
  const WHITE = 0;
  const GRAY = 1;
  const BLACK = 2;
  const color = new Map<string, number>(ids.map((id) => [id, WHITE]));
  const out = new Map<string, string[]>(ids.map((id) => [id, []]));
  const visit = (n: string) => {
    color.set(n, GRAY);
    for (const d of deps.get(n) ?? []) {
      if (color.get(d) === GRAY) continue; // back-edge → drop to break the cycle
      out.get(n)!.push(d);
      if (color.get(d) === WHITE) visit(d);
    }
    color.set(n, BLACK);
  };
  for (const id of ids) if (color.get(id) === WHITE) visit(id);
  return out;
}

/** Member display labels keyed by role_id, disambiguated A/B… when descriptions collide. Reuses the
 *  shared `designateLabels` core (one source of truth with the roster/editor labeling) so the Gantt
 *  lanes + work-breakdown panel read the same as the editor's assignee badges. */
export function teamMemberLabels(headcount: RoleHeadcount[]): Map<string, string> {
  const labels = designateLabels(headcount, (h) => h.role_description);
  return new Map(headcount.map((h, i) => [h.role_id, labels[i].label]));
}

/** Longest-path rank (PERT column) for every leaf over the dependency graph, via Kahn topo order.
 *  A residual cycle (shouldn't happen post-sanitize) leaves the unreached nodes at rank 0. */
function ranks(ids: string[], deps: Map<string, string[]>): Map<string, number> {
  const indeg = new Map<string, number>(ids.map((id) => [id, 0]));
  const succ = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const id of ids) {
    for (const d of deps.get(id) ?? []) {
      indeg.set(id, (indeg.get(id) ?? 0) + 1);
      succ.get(d)?.push(id);
    }
  }
  const rank = new Map<string, number>(ids.map((id) => [id, 0]));
  const queue = ids.filter((id) => (indeg.get(id) ?? 0) === 0);
  while (queue.length) {
    const id = queue.shift()!;
    for (const s of succ.get(id) ?? []) {
      rank.set(s, Math.max(rank.get(s) ?? 0, (rank.get(id) ?? 0) + 1));
      indeg.set(s, (indeg.get(s) ?? 0) - 1);
      if ((indeg.get(s) ?? 0) === 0) queue.push(s);
    }
  }
  return rank;
}

/** Transitive reduction of the dependency DAG → the minimal edge set (a clean PERT). Drops edge
 *  (u→v) when v is already reachable from u through another predecessor of v. */
function reduceEdges(
  ids: string[],
  deps: Map<string, string[]>,
  rank: Map<string, number>,
): { from: string; to: string }[] {
  // reach[a] = set of nodes reachable from a (excluding a). Computed in reverse-topo via ranks.
  const order = [...ids].sort((a, b) => (rank.get(b) ?? 0) - (rank.get(a) ?? 0));
  const reach = new Map<string, Set<string>>(ids.map((id) => [id, new Set<string>()]));
  for (const id of order) {
    const r = reach.get(id)!;
    for (const d of deps.get(id) ?? []) {
      // edge d → id, so id is reachable from d; fold id's reach into d's.
      const rd = reach.get(d)!;
      rd.add(id);
      for (const x of r) rd.add(x);
    }
  }
  const edges: { from: string; to: string }[] = [];
  for (const id of ids) {
    const preds = deps.get(id) ?? [];
    for (const p of preds) {
      // keep p→id only if no OTHER predecessor q of id is reachable from p (i.e. p→…→q→id exists).
      const redundant = preds.some((q) => q !== p && reach.get(p)?.has(q));
      if (!redundant) edges.push({ from: p, to: id });
    }
  }
  return edges;
}

export function deriveWbsSchedule(
  tree: WbsTaskInput[],
  headcount: RoleHeadcount[],
  opts: { nominalWeeks?: number } = {},
): WbsScheduleResult {
  const leaves = collect(tree);
  if (leaves.length === 0) {
    return { tasks: [], rows: [], edges: [], criticalPath: [], totalWeeks: 0 };
  }
  const ids = leaves.map((l) => l.id);
  // The effective leaf dependency graph (leaf edges + package-expanded), then forced acyclic so the
  // greedy scheduler can't deadlock and mis-place a task ahead of its predecessors.
  const deps = breakCycles(ids, effectiveLeafDeps(tree));
  const rank = ranks(ids, deps);
  const labels = teamMemberLabels(headcount);
  const capacity = new Map<string, number>();
  for (const h of headcount) {
    capacity.set(h.role_id, Math.max(1, Math.min(MAX_PARALLEL_SLOTS, Math.round(h.headcount || 1))));
  }

  // --- greedy resource-leveled list scheduling -------------------------------------------
  const durRaw = new Map<string, number>(leaves.map((l) => [l.id, l.hours / WORK_HOURS_PER_WEEK]));
  const indeg = new Map<string, number>(ids.map((id) => [id, (deps.get(id) ?? []).length]));
  const start = new Map<string, number>();
  const finish = new Map<string, number>();
  const slotOf = new Map<string, number>();
  const binding = new Map<string, string | null>();
  // Per-role free-time + last-task per parallel slot.
  const slotFree = new Map<string, number[]>();
  const slotLast = new Map<string, (string | null)[]>();
  const slotsFor = (role: string) => {
    if (!slotFree.has(role)) {
      const cap = capacity.get(role) ?? 1;
      slotFree.set(role, new Array(cap).fill(0));
      slotLast.set(role, new Array(cap).fill(null));
    }
    return { free: slotFree.get(role)!, last: slotLast.get(role)! };
  };

  // Successor adjacency (built once) + an incrementally-maintained ready set turn the scheduler from
  // O(N²·avgDeps) — re-filtering every leaf for readiness and re-scanning every leaf to decrement
  // indegrees each pass — into ~O(N·frontier + E). The greedy pick (earliest possible start) still
  // scans the current ready frontier, but never the whole leaf list. Scheduling decisions are
  // identical to the previous filter-based version (same ready set, same tie-breaks, same indeg math).
  const byId = new Map<string, RawLeaf>(leaves.map((l) => [l.id, l]));
  const succ = new Map<string, string[]>();
  for (const l of leaves) {
    for (const d of deps.get(l.id) ?? []) {
      const arr = succ.get(d);
      if (arr) arr.push(l.id);
      else succ.set(d, [l.id]);
    }
  }

  // earliest dependency finish for a leaf (0 when it has none scheduled yet) + its binding predecessor.
  const depFinish = (l: RawLeaf) => {
    let best = 0;
    let pred: string | null = null;
    for (const d of deps.get(l.id) ?? []) {
      const f = finish.get(d) ?? 0;
      if (f >= best) {
        best = f;
        pred = d;
      }
    }
    return { time: best, pred };
  };
  const candidateStart = (l: RawLeaf) => {
    const { free } = slotsFor(l.roleId);
    // reduce (not Math.min(...free)) so a large-capacity role can't blow the call-arg limit.
    return Math.max(depFinish(l).time, free.reduce((a, b) => Math.min(a, b), Infinity));
  };

  const scheduled = new Set<string>();
  const ready = new Set<string>(); // unscheduled tasks with indegree 0 (the schedulable frontier)
  for (const l of leaves) if ((indeg.get(l.id) ?? 0) === 0) ready.add(l.id);

  while (scheduled.size < leaves.length) {
    let pool: RawLeaf[];
    if (ready.size > 0) {
      pool = [];
      for (const id of ready) pool.push(byId.get(id)!);
    } else {
      // Residual cycle safety net: take the lowest-rank unscheduled task and proceed.
      const residual = leaves
        .filter((l) => !scheduled.has(l.id))
        .sort((a, b) => a.index - b.index);
      if (residual.length === 0) break;
      pool = [residual[0]];
    }

    // Pick the ready task that can start earliest; tie-break longest-first, then document order.
    const chosen = pool.reduce((a, b) => {
      const ca = candidateStart(a);
      const cb = candidateStart(b);
      if (ca !== cb) return ca < cb ? a : b;
      if (a.hours !== b.hours) return a.hours > b.hours ? a : b;
      return a.index < b.index ? a : b;
    });

    const { free, last } = slotsFor(chosen.roleId);
    let slotIdx = 0;
    for (let i = 1; i < free.length; i++) if (free[i] < free[slotIdx]) slotIdx = i;
    const dep = depFinish(chosen);
    const slotTime = free[slotIdx];
    const s = Math.max(dep.time, slotTime);
    const f = s + (durRaw.get(chosen.id) ?? 0);
    start.set(chosen.id, s);
    finish.set(chosen.id, f);
    slotOf.set(chosen.id, slotIdx);
    binding.set(
      chosen.id,
      dep.time >= slotTime ? (dep.time > 0 ? dep.pred : null) : last[slotIdx],
    );
    free[slotIdx] = f;
    last[slotIdx] = chosen.id;
    scheduled.add(chosen.id);
    ready.delete(chosen.id);
    // Only the chosen task's successors can newly become ready — decrement their indegree (not every
    // leaf's), and promote any that reach 0 into the frontier.
    for (const sId of succ.get(chosen.id) ?? []) {
      if (scheduled.has(sId)) continue;
      const nd = (indeg.get(sId) ?? 0) - 1;
      indeg.set(sId, nd);
      if (nd === 0) ready.add(sId);
    }
  }

  // --- scale to the reported duration + mark the critical chain ---------------------------
  const rawMakespan = Math.max(0, ...ids.map((id) => finish.get(id) ?? 0));
  const nominal = opts.nominalWeeks ?? 0;
  const scale = nominal > 0 && rawMakespan > 0 ? nominal / rawMakespan : 1;

  const critical = new Set<string>();
  if (rawMakespan > 0) {
    let cur: string | null = ids.reduce((a, b) => ((finish.get(b) ?? 0) > (finish.get(a) ?? 0) ? b : a));
    while (cur) {
      critical.add(cur);
      cur = binding.get(cur) ?? null;
    }
  }

  const tasks: WbsScheduledTask[] = leaves.map((l) => ({
    id: l.id,
    name: l.name,
    phase: l.phase,
    roleId: l.roleId,
    memberLabel: labels.get(l.roleId) ?? l.roleId ?? "Unassigned",
    hours: l.hours,
    startWeek: (start.get(l.id) ?? 0) * scale,
    endWeek: (finish.get(l.id) ?? 0) * scale,
    durationWeeks: (durRaw.get(l.id) ?? 0) * scale,
    slot: slotOf.get(l.id) ?? 0,
    deps: deps.get(l.id) ?? [],
    rank: rank.get(l.id) ?? 0,
    isCritical: critical.has(l.id),
  }));
  const taskById = new Map(tasks.map((t) => [t.id, t]));

  // --- group into member-slot Gantt rows --------------------------------------------------
  const roleOrder: string[] = [];
  for (const l of leaves) if (!roleOrder.includes(l.roleId)) roleOrder.push(l.roleId);
  const rows: WbsGanttRow[] = [];
  for (const roleId of roleOrder) {
    const roleTasks = tasks.filter((t) => t.roleId === roleId);
    const usedSlots = [...new Set(roleTasks.map((t) => t.slot))].sort((a, b) => a - b);
    usedSlots.forEach((slot, i) => {
      rows.push({
        roleId,
        memberLabel: labels.get(roleId) ?? roleId ?? "Unassigned",
        slot,
        firstOfMember: i === 0,
        tasks: roleTasks.filter((t) => t.slot === slot).sort((a, b) => a.startWeek - b.startWeek),
      });
    });
  }

  const criticalPath = [...critical]
    .map((id) => taskById.get(id)!)
    .sort((a, b) => a.startWeek - b.startWeek)
    .map((t) => t.id);

  return {
    tasks,
    rows,
    edges: reduceEdges(ids, deps, rank),
    criticalPath,
    totalWeeks: rawMakespan * scale,
  };
}

/** WBS (Work Breakdown Structure) wire types + a pure client-side PERT rollup.
 *
 * The authoritative rollup runs on the backend (Monte Carlo); these helpers give the editor an
 * instant per-branch subtotal + project total while the user types, before the server re-evaluates.
 */

import type { Stage2Input, Stage3Input } from "./schemas";
import type { LlmUsage, MissingTask, Phase } from "./types";

export interface WbsTaskInput {
  id: string;
  name: string;
  description?: string;
  /** Leaf-only fields (null/absent on branches). */
  phase?: Phase | null;
  role_id?: string | null;
  optimistic?: number | null;
  most_likely?: number | null;
  pessimistic?: number | null;
  /** "Depends on" predecessor ids. Applies to BOTH kinds: a work package may depend on other work
   *  packages, a task on other tasks. Same-kind/existence is enforced by the editor's option list
   *  and re-sanitized server-side. */
  depends_on?: string[];
  children: WbsTaskInput[];
}

export interface WbsDraftResponse {
  draft_id: string;
  tree: WbsTaskInput[];
  notes: string;
  /** Token cost of the planner call that drafted this tree (absent when no API key / not captured). */
  llm_usage?: LlmUsage | null;
}

export interface WbsDraft {
  draft_id: string;
  project_name: string;
  raw_input: string;
  tree: WbsTaskInput[];
  stage2?: Stage2Input | null;
  stage3?: Stage3Input | null;
  contingency_pct?: number | null;
  /** Token cost of the planner call that drafted this tree, persisted with the draft. */
  llm_usage?: LlmUsage | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface WbsDraftSummary {
  draft_id: string;
  project_name: string;
  task_count: number;
  updated_at: string | null;
}

export interface WbsDraftList {
  items: WbsDraftSummary[];
  resumable: boolean;
}

export interface ThreePoint {
  optimistic: number;
  most_likely: number;
  pessimistic: number;
}

export function isLeaf(node: WbsTaskInput): boolean {
  return !node.children || node.children.length === 0;
}

/** Canonical phase order — shared by the WBS editor (phase picker) and review panel (legend). */
export const PHASE_ORDER: Phase[] = [
  "discovery",
  "ux_design",
  "development",
  "code_review",
  "deployment",
  "qa_testing",
];

/** All branch (non-leaf) ids in the tree — used to seed the tree view's expanded set. */
export function branchIds(nodes: WbsTaskInput[]): string[] {
  return nodes.flatMap((n) => (isLeaf(n) ? [] : [n.id, ...branchIds(n.children)]));
}

/** A branch (work package) whose every task was removed. It has no children AND carries no
 *  leaf data (phase), so it is neither a valid leaf nor a valid branch — the editor prunes
 *  these on removal so autosave never PUTs a node the backend rejects (422). A real leaf is
 *  excluded because it always carries a `phase`. */
export function isEmptyBranch(node: WbsTaskInput): boolean {
  return (!node.children || node.children.length === 0) && !node.phase;
}

/** Coerce a numeric-input string to a valid, non-negative hour value.
 *  Empty / non-numeric / NaN → 0; negatives (which `min={0}` doesn't block on typed
 *  input) are clamped up to 0 so a leaf never carries an invalid negative hour. */
export function clampHours(raw: string): number {
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0) return 0;
  return n;
}

/** Beta-PERT mean of a single three-point estimate. */
export function pertMean(o: number, m: number, p: number): number {
  return (o + 4 * m + p) / 6;
}

/** Comonotonic three-point rollup of a subtree (sum of leaf optimistic / most_likely / pessimistic).
 *  A quick, dependency-free readout for the editor — the backend's independent Monte Carlo combine
 *  is narrower and authoritative. */
export function rollupRange(nodes: WbsTaskInput[]): ThreePoint {
  const acc: ThreePoint = { optimistic: 0, most_likely: 0, pessimistic: 0 };
  for (const n of nodes) {
    if (isLeaf(n)) {
      acc.optimistic += n.optimistic ?? 0;
      acc.most_likely += n.most_likely ?? 0;
      acc.pessimistic += n.pessimistic ?? 0;
    } else {
      const sub = rollupRange(n.children);
      acc.optimistic += sub.optimistic;
      acc.most_likely += sub.most_likely;
      acc.pessimistic += sub.pessimistic;
    }
  }
  return acc;
}

/** Rescale every leaf's 3-point hours by its phase's factor from `byPhase` (default 1 — unchanged),
 *  returning a NEW tree (structure, ids, deps, roles, names all preserved; only the magnitudes move).
 *  Uniform scaling per leaf keeps the optimistic ≤ most_likely ≤ pessimistic ordering and the
 *  within-phase task ratios — it stays a bottom-up rollup, just calibrated. Used by the editor's
 *  "Apply calibration" action to anchor the tree toward the parametric estimate. Pure. */
export function scaleLeafHoursByPhase(
  tree: WbsTaskInput[],
  byPhase: Record<string, number>,
): WbsTaskInput[] {
  const round1 = (n: number) => Math.round(Math.max(0, n) * 10) / 10;
  const scale = (node: WbsTaskInput): WbsTaskInput => {
    if (isLeaf(node)) {
      const f = (node.phase && byPhase[node.phase]) || 1;
      if (f === 1) return node;
      return {
        ...node,
        optimistic: round1((node.optimistic ?? 0) * f),
        most_likely: round1((node.most_likely ?? 0) * f),
        pessimistic: round1((node.pessimistic ?? 0) * f),
      };
    }
    return { ...node, children: node.children.map(scale) };
  };
  return tree.map(scale);
}

/** Sum of a subtree's leaf most-likely hours (the deterministic mid). */
export function subtreeMostLikely(node: WbsTaskInput): number {
  if (isLeaf(node)) return node.most_likely ?? 0;
  return node.children.reduce((a, c) => a + subtreeMostLikely(c), 0);
}

/** Every node's rolled-up most-likely hours, computed in ONE bottom-up pass (O(n)).
 *  Replaces per-node `subtreeMostLikely` calls in the editor/panel renderers, which re-walk
 *  each node's subtree → O(n²) and re-run on every keystroke. Look up `map.get(node.id) ?? 0`. */
export function rolledHoursMap(tree: WbsTaskInput[]): Map<string, number> {
  const map = new Map<string, number>();
  const visit = (node: WbsTaskInput): number => {
    let hours: number;
    if (isLeaf(node)) {
      hours = node.most_likely ?? 0;
    } else {
      hours = node.children.reduce((a, c) => a + visit(c), 0);
    }
    map.set(node.id, hours);
    return hours;
  };
  for (const n of tree) visit(n);
  return map;
}

export function countLeaves(nodes: WbsTaskInput[]): number {
  return nodes.reduce((a, n) => a + (isLeaf(n) ? 1 : countLeaves(n.children)), 0);
}

/** Per-node **labor** cost rolled up bottom-up: a leaf's cost is its most-likely hours × the
 *  assigned member's hourly rate, optionally discounted by the phase's AI reduction for the
 *  AI-assisted scenario (`hours × rate × (1 − reduction)`); a branch is the sum of its descendants.
 *  Mirrors the rollup's BASE-labor basis (most-likely mode + per-phase reduction + explicit role
 *  rates) — i.e. the figure BEFORE the project-level Brooks coordination overhead and contingency
 *  reserve the headline Total adds; the panel surfaces those as separate reconciliation lines.
 *
 *  A role missing from the rate card falls back to `opts.fallbackRate` (default 0), mirroring the
 *  server which remaps an unknown role to a costed default rather than charging it $0. */
export function rolledCostMap(
  tree: WbsTaskInput[],
  rateByRoleId: Map<string, number>,
  opts: {
    reductionByPhase?: Partial<Record<Phase, number>>;
    aiAssisted?: boolean;
    fallbackRate?: number;
  } = {},
): Map<string, number> {
  const map = new Map<string, number>();
  const visit = (node: WbsTaskInput): number => {
    let cost: number;
    if (isLeaf(node)) {
      const rate = (node.role_id ? rateByRoleId.get(node.role_id) : undefined) ?? opts.fallbackRate ?? 0;
      const reductionPct =
        opts.aiAssisted && node.phase ? opts.reductionByPhase?.[node.phase] ?? 0 : 0;
      const factor = opts.aiAssisted ? 1 - reductionPct / 100 : 1;
      cost = (node.most_likely ?? 0) * rate * factor;
    } else {
      cost = node.children.reduce((a, c) => a + visit(c), 0);
    }
    map.set(node.id, cost);
    return cost;
  };
  for (const n of tree) visit(n);
  return map;
}

/** Team-member (people) count per node: a leaf contributes its role's headcount (capacity), a
 *  branch the sum over the DISTINCT roles in its subtree. Matches the Timeline Gantt, which expands
 *  each role into `headcount` parallel lanes — so "N members" on a package agrees with its lanes.
 *  Pass the per-role headcount; an unknown role (or absent map) counts as 1. */
export function memberCountMap(
  tree: WbsTaskInput[],
  headcountByRole: Map<string, number> = new Map(),
): Map<string, number> {
  const map = new Map<string, number>();
  const visit = (node: WbsTaskInput): Set<string> => {
    let roles: Set<string>;
    if (isLeaf(node)) {
      roles = new Set(node.role_id ? [node.role_id] : []);
    } else {
      roles = new Set();
      for (const c of node.children) for (const r of visit(c)) roles.add(r);
    }
    let people = 0;
    for (const r of roles) people += Math.max(1, Math.round(headcountByRole.get(r) ?? 1));
    map.set(node.id, people);
    return roles;
  };
  for (const n of tree) visit(n);
  return map;
}

/** A fresh task id. Uses crypto.randomUUID where available (browser + Node ≥19), else a fallback. */
export function newTaskId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `t-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
}

/** A new, valid leaf with sensible defaults so autosave never rejects an incomplete node. */
export function newLeaf(phase: Phase, roleId: string): WbsTaskInput {
  return {
    id: newTaskId(),
    name: "New task",
    phase,
    role_id: roleId,
    optimistic: 4,
    most_likely: 8,
    pessimistic: 16,
    children: [],
  };
}

/** A new work package (branch) seeded with one blank task. */
export function newPackage(phase: Phase, roleId: string): WbsTaskInput {
  return { id: newTaskId(), name: "New work package", children: [newLeaf(phase, roleId)] };
}

/** The package the completeness critic's accepted suggestions are grouped under. */
export const ADDITIONS_PACKAGE_NAME = "Recommended additions";

/** Insert a completeness-critic suggestion as a new leaf — its title/phase/3-point hours — grouped
 *  under a single "Recommended additions" work package (created on first add). Returns a new tree;
 *  pure except for the fresh ids. */
export function addMissingTask(
  tree: WbsTaskInput[],
  missing: MissingTask,
  roleId: string,
): WbsTaskInput[] {
  const leaf: WbsTaskInput = {
    id: newTaskId(),
    name: missing.title,
    phase: missing.phase,
    role_id: roleId,
    optimistic: missing.optimistic,
    most_likely: missing.most_likely,
    pessimistic: missing.pessimistic,
    children: [],
  };
  const idx = tree.findIndex((n) => !isLeaf(n) && n.name === ADDITIONS_PACKAGE_NAME);
  if (idx >= 0) {
    return tree.map((n, i) => (i === idx ? { ...n, children: [...n.children, leaf] } : n));
  }
  return [...tree, { id: newTaskId(), name: ADDITIONS_PACKAGE_NAME, children: [leaf] }];
}

// --- immutable tree edits (shared by the treemap editor; pure + tested) --------------------

/** Depth-first find of a node by id. */
export function findNode(nodes: WbsTaskInput[], id: string): WbsTaskInput | null {
  for (const n of nodes) {
    if (n.id === id) return n;
    const found = findNode(n.children, id);
    if (found) return found;
  }
  return null;
}

/** Immutably apply a partial patch to the node with `id`. */
export function updateNode(
  nodes: WbsTaskInput[],
  id: string,
  patch: Partial<WbsTaskInput>,
): WbsTaskInput[] {
  return nodes.map((n) =>
    n.id === id ? { ...n, ...patch } : { ...n, children: updateNode(n.children, id, patch) },
  );
}

/** Remove the node with `id`, then prune any branch left empty by the removal (so deleting the
 *  last task in a package removes the now-empty package too — a backend-invalid node otherwise). */
export function removeNode(nodes: WbsTaskInput[], id: string): WbsTaskInput[] {
  return nodes
    .filter((n) => n.id !== id)
    .map((n) => ({ ...n, children: removeNode(n.children, id) }))
    .filter((n) => !isEmptyBranch(n));
}

/** Append `child` under the branch `parentId`. If the parent was a leaf, it becomes a branch —
 *  its leaf fields are cleared so the result stays backend-valid (a branch carries no estimate). */
export function addChild(
  nodes: WbsTaskInput[],
  parentId: string,
  child: WbsTaskInput,
): WbsTaskInput[] {
  return nodes.map((n) => {
    if (n.id === parentId) {
      return {
        ...n,
        children: [...n.children, child],
        phase: null,
        role_id: null,
        optimistic: null,
        most_likely: null,
        pessimistic: null,
        // The node flips leaf→branch, so its task-kind dependencies no longer apply (a work package
        // depends only on work packages). Inbound task→this edges are pruned server-side.
        depends_on: [],
      };
    }
    return { ...n, children: addChild(n.children, parentId, child) };
  });
}

/** All ids in a node's subtree (itself + descendants) — used to forbid moving a node into itself. */
export function subtreeIds(node: WbsTaskInput): Set<string> {
  const ids = new Set<string>([node.id]);
  for (const c of node.children) for (const id of subtreeIds(c)) ids.add(id);
  return ids;
}

/** Reparent node `id` under `newParentId` (null = top level). No-ops on a cycle (moving into self
 *  or a descendant). If detaching empties (and prunes) the intended parent, falls back to top level. */
export function moveNode(
  nodes: WbsTaskInput[],
  id: string,
  newParentId: string | null,
): WbsTaskInput[] {
  const node = findNode(nodes, id);
  if (!node) return nodes;
  if (newParentId !== null && subtreeIds(node).has(newParentId)) return nodes; // cycle
  const without = removeNode(nodes, id);
  if (newParentId !== null && findNode(without, newParentId)) {
    return addChild(without, newParentId, node);
  }
  return [...without, node]; // top level (or the target was pruned away)
}

/** Branch nodes a node may be moved under (excludes itself + its descendants + all leaves), as
 *  `{id, name}`. The UI adds a synthetic "Top level" option separately. */
export function moveTargets(
  tree: WbsTaskInput[],
  id: string,
): { id: string; name: string }[] {
  const node = findNode(tree, id);
  const banned = node ? subtreeIds(node) : new Set<string>([id]);
  const out: { id: string; name: string }[] = [];
  const walk = (nodes: WbsTaskInput[]) => {
    for (const n of nodes) {
      if (!isLeaf(n) && !banned.has(n.id)) out.push({ id: n.id, name: n.name });
      walk(n.children);
    }
  };
  walk(tree);
  return out;
}

/** The effective leaf-level dependency graph: each leaf's predecessor leaf ids = its own
 *  `depends_on` (to existing leaves) PLUS, for every ancestor work package that depends on another
 *  package, all of that package's leaves. This is the graph the schedule actually enforces, so it's
 *  the one the editor must use for cycle detection too — leaf-only edges miss package-implied
 *  orderings. Self / unknown ids are dropped. Returns `leafId → predecessor leaf ids`. */
export function effectiveLeafDeps(tree: WbsTaskInput[]): Map<string, string[]> {
  const leafIds = new Set<string>();
  const branchLeaves = new Map<string, string[]>(); // branch id → its descendant leaf ids
  const branchDeps = new Map<string, string[]>();
  const leaves: { id: string; ownDeps: string[]; ancestors: string[] }[] = [];

  const walk = (nodes: WbsTaskInput[], ancestors: string[]): string[] => {
    const collected: string[] = [];
    for (const n of nodes) {
      if (isLeaf(n)) {
        leafIds.add(n.id);
        leaves.push({ id: n.id, ownDeps: [...(n.depends_on ?? [])], ancestors });
        collected.push(n.id);
      } else {
        branchDeps.set(n.id, [...(n.depends_on ?? [])]);
        const child = walk(n.children, [...ancestors, n.id]);
        branchLeaves.set(n.id, child);
        collected.push(...child);
      }
    }
    return collected;
  };
  walk(tree, []);

  const deps = new Map<string, string[]>();
  for (const leaf of leaves) {
    const set = new Set<string>();
    for (const d of leaf.ownDeps) if (leafIds.has(d) && d !== leaf.id) set.add(d);
    for (const pkg of leaf.ancestors) {
      for (const pkgDep of branchDeps.get(pkg) ?? []) {
        for (const pl of branchLeaves.get(pkgDep) ?? []) if (pl !== leaf.id) set.add(pl);
      }
    }
    deps.set(leaf.id, [...set]);
  }
  return deps;
}

/** Nodes that node `id` may declare a "depends on" relationship to: the SAME kind (a work package
 *  depends only on work packages, a task only on tasks), excluding itself and any node that already
 *  (transitively) depends on `id` — adding that edge would close a cycle. Returned as `{id, name}`
 *  in document order. Returns `[]` when `id` isn't found. */
export function dependencyTargets(
  tree: WbsTaskInput[],
  id: string,
): { id: string; name: string }[] {
  const self = findNode(tree, id);
  if (!self) return [];
  const wantLeaf = isLeaf(self);

  // Reverse adjacency (dep → dependent) over the EFFECTIVE graph for the selected kind: leaves use
  // the package-expanded leaf graph (so package-implied orderings count — a leaf-only check would
  // offer a predecessor the package edges already order after `id`, a hidden cycle); branches use
  // the package→package graph. BFS from `id` finds every node that already (transitively) depends on
  // it; offering any of those as a predecessor would close a cycle.
  const reverse = new Map<string, string[]>();
  const addEdge = (dep: string, dependent: string) => {
    const arr = reverse.get(dep) ?? [];
    arr.push(dependent);
    reverse.set(dep, arr);
  };
  if (wantLeaf) {
    for (const [leaf, deps] of effectiveLeafDeps(tree)) for (const d of deps) addEdge(d, leaf);
  } else {
    const walk = (nodes: WbsTaskInput[]) => {
      for (const n of nodes) {
        if (!isLeaf(n)) for (const d of n.depends_on ?? []) addEdge(d, n.id);
        walk(n.children);
      }
    };
    walk(tree);
  }

  const cyclic = new Set<string>();
  const queue = [id];
  while (queue.length) {
    const cur = queue.shift()!;
    for (const dependent of reverse.get(cur) ?? []) {
      if (!cyclic.has(dependent)) {
        cyclic.add(dependent);
        queue.push(dependent);
      }
    }
  }

  const out: { id: string; name: string }[] = [];
  const walk = (nodes: WbsTaskInput[]) => {
    for (const n of nodes) {
      if (n.id !== id && isLeaf(n) === wantLeaf && !cyclic.has(n.id)) {
        out.push({ id: n.id, name: n.name });
      }
      walk(n.children);
    }
  };
  walk(tree);
  return out;
}

/** Drop every `depends_on` reference that is no longer valid: an id absent from the tree (e.g. a
 *  deleted predecessor, possibly cascading to a pruned empty package) OR a cross-kind reference
 *  (a work package may depend only on packages, a task only on tasks — a leaf→branch flip can leave
 *  one behind). Keeps the graph referentially valid AND same-kind so autosave never sends an edge
 *  the backend would silently drop (which would lose it on resume). */
export function pruneDanglingDependencies(nodes: WbsTaskInput[]): WbsTaskInput[] {
  const kindById = new Map<string, boolean>(); // id → isLeaf
  const collect = (ns: WbsTaskInput[]) => {
    for (const n of ns) {
      kindById.set(n.id, isLeaf(n));
      collect(n.children);
    }
  };
  collect(nodes);

  const scrub = (ns: WbsTaskInput[]): WbsTaskInput[] =>
    ns.map((n) => ({
      ...n,
      // `=== isLeaf(n)` drops both a dangling id (get → undefined) and a cross-kind one.
      depends_on: n.depends_on?.filter((d) => kindById.get(d) === isLeaf(n)),
      children: scrub(n.children),
    }));
  return scrub(nodes);
}

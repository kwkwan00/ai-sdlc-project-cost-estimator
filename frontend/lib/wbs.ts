/** WBS (Work Breakdown Structure) wire types + a pure client-side PERT rollup.
 *
 * The authoritative rollup runs on the backend (Monte Carlo); these helpers give the editor an
 * instant per-branch subtotal + project total while the user types, before the server re-evaluates.
 */

import type { Stage2Input, Stage3Input } from "./schemas";
import type { Phase } from "./types";

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
  children: WbsTaskInput[];
}

export interface WbsDraftResponse {
  draft_id: string;
  tree: WbsTaskInput[];
  notes: string;
}

export interface WbsDraft {
  draft_id: string;
  project_name: string;
  raw_input: string;
  tree: WbsTaskInput[];
  stage2?: Stage2Input | null;
  stage3?: Stage3Input | null;
  contingency_pct?: number | null;
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

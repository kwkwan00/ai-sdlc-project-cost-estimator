"use client";

import { SimpleTreeView } from "@mui/x-tree-view/SimpleTreeView";
import { TreeItem } from "@mui/x-tree-view/TreeItem";
import { useMemo } from "react";

import { PHASE_LABELS } from "@/lib/types";
import { branchIds, isLeaf, PHASE_ORDER, rolledHoursMap, type WbsTaskInput } from "@/lib/wbs";
import { PHASE_COLORS, PHASE_FALLBACK_COLOR } from "@/lib/wbs-colors";

/** Read-only WBS tree on the review page, rendered as a MUI X Tree View. Branches show their
 *  rolled-up most-likely hours; leaves show their phase, role, and 3-point estimate inline. */
export function WbsTreePanel({ tree }: { tree: WbsTaskInput[] }) {
  // One O(n) bottom-up pass for branch rollups + one for the expanded set, instead of per-node
  // O(n²) subtree re-walks on every render.
  const hours = useMemo(() => rolledHoursMap(tree), [tree]);
  const expanded = useMemo(() => branchIds(tree), [tree]);

  if (tree.length === 0) {
    return <p className="muted text-sm">No work breakdown to display.</p>;
  }

  const renderItem = (node: WbsTaskInput) => (
    <TreeItem
      key={node.id}
      itemId={node.id}
      label={
        <div className="flex items-center justify-between gap-2 py-0.5">
          <span className="flex min-w-0 items-center gap-2">
            {isLeaf(node) && node.phase && (
              <span
                className="h-2.5 w-2.5 shrink-0 rounded-sm"
                style={{ backgroundColor: PHASE_COLORS[node.phase] ?? PHASE_FALLBACK_COLOR }}
              />
            )}
            <span className="truncate text-sm">{node.name}</span>
            {isLeaf(node) && node.role_id && (
              <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
                {node.role_id}
              </span>
            )}
          </span>
          <span className="shrink-0 text-xs text-slate-400">
            {isLeaf(node)
              ? `${node.optimistic ?? 0} / ${node.most_likely ?? 0} / ${node.pessimistic ?? 0} h`
              : `${Math.round(hours.get(node.id) ?? 0)} h`}
          </span>
        </div>
      }
    >
      {node.children.map(renderItem)}
    </TreeItem>
  );

  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-slate-200 p-2">
        <SimpleTreeView defaultExpandedItems={expanded}>
          {tree.map(renderItem)}
        </SimpleTreeView>
      </div>

      {/* Phase legend */}
      <div className="flex flex-wrap gap-3">
        {PHASE_ORDER.map((p) => (
          <span key={p} className="flex items-center gap-1.5 text-xs muted">
            <span className="h-3 w-3 rounded-sm" style={{ backgroundColor: PHASE_COLORS[p] }} />
            {PHASE_LABELS[p]}
          </span>
        ))}
      </div>
      <p className="text-xs muted">Leaf rows show optimistic / most-likely / pessimistic hours.</p>
    </div>
  );
}

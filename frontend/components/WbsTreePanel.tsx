"use client";

import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import IconButton from "@mui/material/IconButton";
import { SimpleTreeView } from "@mui/x-tree-view/SimpleTreeView";
import { TreeItem } from "@mui/x-tree-view/TreeItem";
import { useMemo, useState } from "react";

import { Modal } from "@/components/Modal";
import { formatUSD } from "@/lib/format";
import { PHASE_LABELS, type Phase, type RoleHeadcount } from "@/lib/types";
import {
  branchIds,
  isLeaf,
  memberCountMap,
  pertMean,
  PHASE_ORDER,
  rolledCostMap,
  rolledHoursMap,
  type WbsTaskInput,
} from "@/lib/wbs";
import { PHASE_COLORS, PHASE_FALLBACK_COLOR } from "@/lib/wbs-colors";
import { teamMemberLabels } from "@/lib/wbs-schedule";

interface Props {
  tree: WbsTaskInput[];
  /** Team roster (for member labels + hourly rates), from the estimate's `headcount_by_role`. */
  headcount: RoleHeadcount[];
  /** Per-phase AI effort reduction %, used to discount task cost in the AI-assisted scenario. */
  reductionByPhase: Partial<Record<Phase, number>>;
  mode: "ai_assisted" | "manual_only";
  /** Project-level uplifts the headline Total adds on top of labor — surfaced as reconciliation
   *  lines so the per-package costs add up to the Total card (not silently fall short). */
  brooksOverheadPct?: number;
  contingencyPct?: number;
  /** Rate charged to a leaf whose role_id isn't in the rate card — mirrors the server, which remaps
   *  an unknown role to a costed default rather than charging $0. */
  fallbackRate?: number;
}

/** Read-only WBS tree on the review page, rendered as a MUI X Tree View. Leaf rows are kept compact
 *  (phase + name + member + cost); a details (ⓘ) icon opens a read-only modal — mirroring the Edit
 *  WBS task editor — with the full phase / member / 3-point effort / cost / description /
 *  dependencies. Each work package shows how many members it spans, its rolled-up hours, and labor
 *  cost. Costs follow the active scenario (AI-assisted discounts each task by its phase's reduction);
 *  a footer reconciles the labor subtotal to the headline Total via the Brooks overhead +
 *  contingency uplifts (which are project-level, not per-task). */
export function WbsTreePanel({
  tree,
  headcount,
  reductionByPhase,
  mode,
  brooksOverheadPct = 0,
  contingencyPct = 0,
  fallbackRate = 0,
}: Props) {
  const aiAssisted = mode === "ai_assisted";
  const [detail, setDetail] = useState<WbsTaskInput | null>(null);

  // One O(n) bottom-up pass each for rollups + one for the expanded set, instead of per-node O(n²)
  // subtree re-walks on every render.
  const hours = useMemo(() => rolledHoursMap(tree), [tree]);
  const expanded = useMemo(() => branchIds(tree), [tree]);
  const labelById = useMemo(() => teamMemberLabels(headcount), [headcount]);
  const rateById = useMemo(
    () => new Map(headcount.map((h) => [h.role_id, h.rate_per_hour])),
    [headcount],
  );
  const headcountByRole = useMemo(
    () => new Map(headcount.map((h) => [h.role_id, h.headcount])),
    [headcount],
  );
  const cost = useMemo(
    () => rolledCostMap(tree, rateById, { reductionByPhase, aiAssisted, fallbackRate }),
    [tree, rateById, reductionByPhase, aiAssisted, fallbackRate],
  );
  const memberCounts = useMemo(() => memberCountMap(tree, headcountByRole), [tree, headcountByRole]);
  // id → name, to resolve a leaf's depends_on ids to readable predecessor names in the modal.
  const nameById = useMemo(() => {
    const m = new Map<string, string>();
    const walk = (ns: WbsTaskInput[]) => {
      for (const n of ns) {
        m.set(n.id, n.name);
        walk(n.children);
      }
    };
    walk(tree);
    return m;
  }, [tree]);

  // Reconcile the labor subtotal (Σ top-level node costs) to the headline Total: the rollup multiplies
  // base labor by (1 + Brooks overhead) then (1 + contingency), so the same uplifts close the gap.
  const laborSubtotal = tree.reduce((s, n) => s + (cost.get(n.id) ?? 0), 0);
  const afterBrooks = laborSubtotal * (1 + brooksOverheadPct / 100);
  const reconciledTotal = afterBrooks * (1 + contingencyPct / 100);

  if (tree.length === 0) {
    return <p className="muted text-sm">No work breakdown to display.</p>;
  }

  const memberLabel = (roleId: string | null | undefined) =>
    roleId ? labelById.get(roleId) ?? roleId : "—";

  const renderItem = (node: WbsTaskInput) => {
    const leaf = isLeaf(node);
    const members = memberCounts.get(node.id) ?? 0;
    return (
      <TreeItem
        key={node.id}
        itemId={node.id}
        label={
          <div className="flex items-center justify-between gap-2 py-0.5">
            <span className="flex min-w-0 items-center gap-2">
              {leaf && node.phase && (
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-sm"
                  style={{ backgroundColor: PHASE_COLORS[node.phase] ?? PHASE_FALLBACK_COLOR }}
                />
              )}
              <span className="truncate text-sm">{node.name}</span>
              {leaf
                ? node.role_id && (
                    <span
                      className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600"
                      title={`Team member: ${memberLabel(node.role_id)}`}
                    >
                      {memberLabel(node.role_id)}
                    </span>
                  )
                : members > 0 && (
                    <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
                      {members} member{members === 1 ? "" : "s"}
                    </span>
                  )}
            </span>
            <span className="flex shrink-0 items-center gap-1.5 text-xs tabular-nums">
              {!leaf && (
                <span className="text-slate-400">{Math.round(hours.get(node.id) ?? 0)} h</span>
              )}
              <span className="w-16 text-right font-medium text-slate-600">
                {formatUSD(cost.get(node.id) ?? 0)}
              </span>
              {leaf && (
                <IconButton
                  size="small"
                  aria-label={`Details for ${node.name}`}
                  title="Task details"
                  onClick={(e) => {
                    e.stopPropagation(); // don't toggle the tree row
                    setDetail(node);
                  }}
                >
                  <InfoOutlinedIcon fontSize="small" />
                </IconButton>
              )}
            </span>
          </div>
        }
      >
        {node.children.map(renderItem)}
      </TreeItem>
    );
  };

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
      {/* Reconcile per-package labor to the headline Total via the project-level uplifts. */}
      {(brooksOverheadPct > 0 || contingencyPct > 0) && (
        <dl className="ml-auto max-w-xs space-y-1 rounded-lg bg-slate-50 p-3 text-xs tabular-nums">
          <div className="flex justify-between gap-4">
            <dt className="muted">Labor subtotal</dt>
            <dd className="text-slate-700">{formatUSD(laborSubtotal)}</dd>
          </div>
          {brooksOverheadPct > 0 && (
            <div className="flex justify-between gap-4">
              <dt className="muted">+ Coordination overhead ({brooksOverheadPct.toFixed(0)}%)</dt>
              <dd className="text-slate-700">{formatUSD(afterBrooks - laborSubtotal)}</dd>
            </div>
          )}
          {contingencyPct > 0 && (
            <div className="flex justify-between gap-4">
              <dt className="muted">+ Contingency reserve ({contingencyPct.toFixed(0)}%)</dt>
              <dd className="text-slate-700">{formatUSD(reconciledTotal - afterBrooks)}</dd>
            </div>
          )}
          <div className="flex justify-between gap-4 border-t border-slate-200 pt-1 font-semibold text-slate-800">
            <dt>Total ({aiAssisted ? "AI-assisted" : "manual-only"})</dt>
            <dd>{formatUSD(reconciledTotal)}</dd>
          </div>
        </dl>
      )}

      <p className="text-xs muted">
        Each leaf shows its phase, assigned team member, and cost; open the ⓘ for full effort &amp;
        dependencies. Cost = most-likely hours × the member&apos;s rate
        {aiAssisted ? ", discounted by each phase's AI reduction" : " (manual-only scenario)"} — this
        is <span className="font-medium">labor</span> only; work packages roll up their tasks, and
        the coordination overhead + contingency that the project Total adds are shown above.
      </p>

      {/* Read-only task details (mirrors the Edit WBS task editor). */}
      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={detail?.name ?? "Task details"}
        widthClass="max-w-lg"
      >
        {detail && (
          <div className="space-y-4 text-sm">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <span className="label">Phase</span>
                <div className="mt-1 flex items-center gap-1.5 text-slate-700">
                  {detail.phase && (
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-sm"
                      style={{ backgroundColor: PHASE_COLORS[detail.phase] ?? PHASE_FALLBACK_COLOR }}
                    />
                  )}
                  {detail.phase ? PHASE_LABELS[detail.phase] : "—"}
                </div>
              </div>
              <div>
                <span className="label">Team member</span>
                <p className="mt-1 text-slate-700">{memberLabel(detail.role_id)}</p>
              </div>
            </div>

            <div>
              <span className="label">Effort — 3-point estimate (hours)</span>
              <div className="mt-1 grid grid-cols-3 gap-2">
                {(
                  [
                    ["Optimistic", detail.optimistic],
                    ["Most likely", detail.most_likely],
                    ["Pessimistic", detail.pessimistic],
                  ] as const
                ).map(([label, value]) => (
                  <div
                    key={label}
                    className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1.5"
                  >
                    <div className="text-[10px] uppercase tracking-wide muted">{label}</div>
                    <div className="tabular-nums text-slate-800">{value ?? 0} h</div>
                  </div>
                ))}
              </div>
              <p className="help mt-1">
                PERT mean ≈{" "}
                <span className="font-medium text-slate-700">
                  {Math.round(
                    pertMean(
                      detail.optimistic ?? 0,
                      detail.most_likely ?? 0,
                      detail.pessimistic ?? 0,
                    ),
                  )}{" "}
                  h
                </span>
              </p>
            </div>

            <div>
              <span className="label">Cost ({aiAssisted ? "AI-assisted" : "manual-only"})</span>
              <p className="mt-1 font-medium text-slate-800">{formatUSD(cost.get(detail.id) ?? 0)}</p>
            </div>

            {detail.description?.trim() && (
              <div>
                <span className="label">Description</span>
                <p className="mt-1 whitespace-pre-wrap text-slate-700">{detail.description}</p>
              </div>
            )}

            <div>
              <span className="label">Depends on</span>
              {detail.depends_on && detail.depends_on.length > 0 ? (
                <ul className="mt-1 list-disc space-y-0.5 pl-5 text-slate-700">
                  {detail.depends_on.map((d) => (
                    <li key={d} className="truncate">
                      {nameById.get(d) ?? d}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1 muted">No prerequisites — can start independently.</p>
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

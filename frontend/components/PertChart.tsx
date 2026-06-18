"use client";

import type { ScheduleResult } from "@/lib/schedule";
import type { Phase } from "@/lib/types";

/** PERT-style dependency network for the Timeline tab. Renders the six phases as nodes laid
 *  out in columns by longest-path rank, with dependency arrows; the critical path is
 *  highlighted, and each node shows its duration, slack, and Monte-Carlo criticality index
 *  (how often it lands on the critical path across the simulated draws). */
const NODE_W = 136;
const NODE_H = 66;
const COL_GAP = 44;
const ROW_GAP = 18;
const PAD = 6;

export function PertChart({ schedule }: { schedule: ScheduleResult }) {
  const { phases, edges, criticalPath } = schedule;
  const visible = phases.filter((p) => p.durationWeeks > 0);
  if (visible.length === 0) {
    return <p className="text-sm muted">No dependency network to display.</p>;
  }
  const visibleSet = new Set(visible.map((p) => p.phase));
  const critSet = new Set(criticalPath);
  const simulated = schedule.risk?.simulated ?? false;

  // Longest-path rank over visible nodes (phases array is already in topological order).
  const rank: Record<string, number> = {};
  for (const p of phases) {
    if (!visibleSet.has(p.phase)) continue;
    const preds = edges.filter((e) => e.to === p.phase && visibleSet.has(e.from));
    rank[p.phase] = preds.length ? Math.max(...preds.map((e) => rank[e.from] + 1)) : 0;
  }

  const cols: Phase[][] = [];
  for (const p of visible) (cols[rank[p.phase]] ??= []).push(p.phase);
  const colHeights = cols.map((c) => c.length * NODE_H + (c.length - 1) * ROW_GAP);
  const maxColH = Math.max(...colHeights);

  const pos: Record<string, { x: number; y: number }> = {};
  cols.forEach((col, ci) => {
    const yStart = PAD + (maxColH - colHeights[ci]) / 2;
    col.forEach((ph, ri) => {
      pos[ph] = { x: PAD + ci * (NODE_W + COL_GAP), y: yStart + ri * (NODE_H + ROW_GAP) };
    });
  });

  const width = PAD * 2 + cols.length * NODE_W + (cols.length - 1) * COL_GAP;
  const height = PAD * 2 + maxColH;
  const visibleEdges = edges.filter((e) => visibleSet.has(e.from) && visibleSet.has(e.to));

  return (
    <div className="overflow-x-auto">
      <div className="relative" style={{ width, height, minWidth: width }}>
        <svg width={width} height={height} className="absolute inset-0 pointer-events-none">
          <defs>
            <marker id="pert-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" fill="#cbd5e1" />
            </marker>
            <marker id="pert-arrow-crit" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" fill="#f59e0b" />
            </marker>
          </defs>
          {visibleEdges.map((e) => {
            const a = pos[e.from];
            const b = pos[e.to];
            const crit = critSet.has(e.from) && critSet.has(e.to);
            const x1 = a.x + NODE_W;
            const y1 = a.y + NODE_H / 2;
            const x2 = b.x;
            const y2 = b.y + NODE_H / 2;
            const mx = (x1 + x2) / 2;
            return (
              <path
                key={`${e.from}-${e.to}`}
                d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
                fill="none"
                stroke={crit ? "#f59e0b" : "#cbd5e1"}
                strokeWidth={crit ? 2 : 1.5}
                markerEnd={`url(#${crit ? "pert-arrow-crit" : "pert-arrow"})`}
              />
            );
          })}
        </svg>

        {visible.map((p) => (
          <div
            key={p.phase}
            className={`absolute rounded-lg border px-2 py-1.5 shadow-sm ${
              p.isCritical ? "border-amber-400 bg-amber-50" : "border-slate-200 bg-white"
            }`}
            style={{ left: pos[p.phase].x, top: pos[p.phase].y, width: NODE_W, height: NODE_H }}
            title={
              p.isCritical
                ? `${p.label}: on the critical path · ${p.durationWeeks.toFixed(1)} wk`
                : `${p.label}: ${p.slackWeeks.toFixed(1)} wk slack · ${p.durationWeeks.toFixed(1)} wk`
            }
          >
            <div className="flex items-center justify-between gap-1">
              <span className="text-[11px] font-medium text-slate-700 truncate">{p.label}</span>
              {p.isCritical && (
                <span className="shrink-0 rounded bg-amber-200 px-1 text-[9px] font-semibold text-amber-800">
                  critical
                </span>
              )}
            </div>
            <div className="mt-0.5 text-[10px] tabular-nums text-slate-500">
              {p.durationWeeks.toFixed(1)} wk
              {!p.isCritical && p.slackWeeks > 1e-6 && (
                <span className="text-slate-400"> · {p.slackWeeks.toFixed(1)} slack</span>
              )}
            </div>
            {simulated && (
              <div className="mt-1 flex items-center gap-1">
                <div className="h-1 flex-1 overflow-hidden rounded-full bg-slate-100">
                  <div
                    className={`h-full ${p.criticalityPct >= 50 ? "bg-amber-500" : "bg-brand-300"}`}
                    style={{ width: `${Math.round(p.criticalityPct)}%` }}
                  />
                </div>
                <span className="text-[9px] tabular-nums text-slate-400">
                  {Math.round(p.criticalityPct)}%
                </span>
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 pt-2 text-[10px] text-slate-400">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-3 rounded-sm bg-amber-400" /> critical path
        </span>
        {simulated && <span>criticality bar = % of Monte-Carlo draws on the critical path</span>}
      </div>
    </div>
  );
}

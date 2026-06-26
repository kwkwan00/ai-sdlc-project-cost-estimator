"use client";

import { useMemo, useState } from "react";

import { PHASE_COLORS, PHASE_FALLBACK_COLOR } from "@/lib/wbs-colors";
import type { WbsScheduledTask, WbsScheduleResult } from "@/lib/wbs-schedule";

/** Task dependency network for the WBS Timeline tab. Tasks are laid out in columns by their
 *  longest-path rank over the `depends_on` graph — so a column is a set of tasks that can run in
 *  parallel — with (transitively reduced) dependency arrows. The critical chain is highlighted, and
 *  each node carries a phase color stripe + its assigned member.
 *
 *  Interactive focus: **hover** a task to light up its direct predecessors/successors, **click** it
 *  to trace its full upstream+downstream dependency chain (everything else dims). Click the
 *  background to clear. This keeps large graphs navigable without changing the layout. */
const NODE_W = 150;
const NODE_H = 56;
const COL_GAP = 46;
const ROW_GAP = 14;
const PAD = 6;

function push(m: Map<string, string[]>, k: string, v: string) {
  const arr = m.get(k);
  if (arr) arr.push(v);
  else m.set(k, [v]);
}

export function WbsPertChart({ schedule }: { schedule: WbsScheduleResult }) {
  const { tasks, edges } = schedule;
  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);

  // Adjacency over the (reachability-preserving) reduced edge set — successors + predecessors.
  const { succ, pred } = useMemo(() => {
    const s = new Map<string, string[]>();
    const p = new Map<string, string[]>();
    for (const e of edges) {
      push(s, e.from, e.to);
      push(p, e.to, e.from);
    }
    return { succ: s, pred: p };
  }, [edges]);

  if (tasks.length === 0) {
    return <p className="text-sm muted">No dependency network to display.</p>;
  }
  const byId = new Map(tasks.map((t) => [t.id, t]));

  // --- focus set (hover = direct neighbors; click = full transitive chain) ---------------
  const focusId = selected ?? hovered;
  const chainMode = selected != null;
  let focusNodes: Set<string> | null = null;
  if (focusId) {
    focusNodes = new Set<string>([focusId]);
    if (chainMode) {
      const walk = (adj: Map<string, string[]>) => {
        const q = [focusId];
        while (q.length) {
          const c = q.shift()!;
          for (const n of adj.get(c) ?? []) {
            if (!focusNodes!.has(n)) {
              focusNodes!.add(n);
              q.push(n);
            }
          }
        }
      };
      walk(pred);
      walk(succ);
    } else {
      for (const n of pred.get(focusId) ?? []) focusNodes.add(n);
      for (const n of succ.get(focusId) ?? []) focusNodes.add(n);
    }
  }
  const nodeActive = (id: string) => !focusNodes || focusNodes.has(id);
  const edgeActive = (from: string, to: string) =>
    !focusNodes
      ? true
      : chainMode
        ? focusNodes.has(from) && focusNodes.has(to)
        : from === focusId || to === focusId;

  // --- layout ----------------------------------------------------------------------------
  const cols: WbsScheduledTask[][] = [];
  for (const t of tasks) (cols[t.rank] ??= []).push(t);
  for (const c of cols) if (c) c.sort((a, b) => a.startWeek - b.startWeek);
  const filled = cols.map((c) => c ?? []);

  const colHeights = filled.map((c) => c.length * NODE_H + Math.max(0, c.length - 1) * ROW_GAP);
  const maxColH = Math.max(NODE_H, ...colHeights);

  const pos: Record<string, { x: number; y: number }> = {};
  filled.forEach((col, ci) => {
    const yStart = PAD + (maxColH - colHeights[ci]) / 2;
    col.forEach((t, ri) => {
      pos[t.id] = { x: PAD + ci * (NODE_W + COL_GAP), y: yStart + ri * (NODE_H + ROW_GAP) };
    });
  });

  const width = PAD * 2 + filled.length * NODE_W + Math.max(0, filled.length - 1) * COL_GAP;
  const height = PAD * 2 + maxColH;

  const toggle = (id: string) => setSelected((cur) => (cur === id ? null : id));

  return (
    <div className="overflow-x-auto">
      <div
        className="relative"
        style={{ width, height, minWidth: width }}
        onClick={() => setSelected(null)}
      >
        <svg width={width} height={height} className="absolute inset-0 pointer-events-none">
          <defs>
            {(
              [
                ["wbs-arrow", "#cbd5e1"],
                ["wbs-arrow-crit", "#f59e0b"],
                ["wbs-arrow-focus", "#6366f1"],
                ["wbs-arrow-dim", "#e2e8f0"],
              ] as const
            ).map(([id, fill]) => (
              <marker key={id} id={id} markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill={fill} />
              </marker>
            ))}
          </defs>
          {edges.map((e) => {
            const a = pos[e.from];
            const b = pos[e.to];
            if (!a || !b) return null;
            const crit = byId.get(e.from)?.isCritical && byId.get(e.to)?.isCritical;
            const active = edgeActive(e.from, e.to);
            const dim = focusNodes != null && !active;
            const x1 = a.x + NODE_W;
            const y1 = a.y + NODE_H / 2;
            const x2 = b.x;
            const y2 = b.y + NODE_H / 2;
            const mx = (x1 + x2) / 2;
            const d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
            // Colour: dim edges fade; focused edges go indigo (or stay amber if on the critical
            // chain); with no focus active, the usual amber/slate.
            const stroke = dim ? "#e2e8f0" : crit ? "#f59e0b" : focusNodes ? "#6366f1" : "#cbd5e1";
            const marker = dim
              ? "wbs-arrow-dim"
              : crit
                ? "wbs-arrow-crit"
                : focusNodes
                  ? "wbs-arrow-focus"
                  : "wbs-arrow";
            return (
              <path
                key={`${e.from}-${e.to}`}
                d={d}
                fill="none"
                stroke={stroke}
                strokeWidth={dim ? 1 : crit || (focusNodes && active) ? 2 : 1.5}
                markerEnd={`url(#${marker})`}
                className="transition-colors"
              />
            );
          })}
        </svg>

        {tasks.map((t) => {
          const color = t.phase ? PHASE_COLORS[t.phase] ?? PHASE_FALLBACK_COLOR : PHASE_FALLBACK_COLOR;
          const active = nodeActive(t.id);
          const isFocus = t.id === focusId;
          return (
            <div
              key={t.id}
              role="button"
              tabIndex={0}
              aria-pressed={selected === t.id}
              onClick={(ev) => {
                ev.stopPropagation();
                toggle(t.id);
              }}
              onMouseEnter={() => setHovered(t.id)}
              onMouseLeave={() => setHovered((h) => (h === t.id ? null : h))}
              onKeyDown={(ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                  ev.preventDefault();
                  toggle(t.id);
                }
              }}
              className={`absolute flex cursor-pointer flex-col overflow-hidden rounded-lg border bg-white shadow-sm transition-opacity focus:outline-none focus:ring-2 focus:ring-brand-400 ${
                isFocus
                  ? "border-brand-500 ring-2 ring-brand-500"
                  : t.isCritical
                    ? "border-amber-400"
                    : "border-slate-200"
              } ${active ? "" : "opacity-30"}`}
              style={{ left: pos[t.id].x, top: pos[t.id].y, width: NODE_W, height: NODE_H }}
              title={`${t.name} · ${t.memberLabel} · wk ${t.startWeek.toFixed(1)}–${t.endWeek.toFixed(1)} (${Math.round(
                t.hours,
              )} h)${t.isCritical ? " · critical chain" : ""}`}
            >
              <span className="h-1 w-full shrink-0" style={{ backgroundColor: color }} />
              <div className="flex min-h-0 flex-1 flex-col justify-center px-2 py-1">
                <div className="flex items-center justify-between gap-1">
                  <span className="truncate text-[11px] font-medium text-slate-700">{t.name}</span>
                  {t.isCritical && (
                    <span className="shrink-0 rounded bg-amber-200 px-1 text-[8px] font-semibold text-amber-800">
                      critical
                    </span>
                  )}
                </div>
                <div className="truncate text-[9px] text-slate-500">{t.memberLabel}</div>
                <div className="text-[9px] tabular-nums text-slate-400">
                  wk {t.startWeek.toFixed(1)}–{t.endWeek.toFixed(1)}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-2 text-[10px] text-slate-400">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-3 rounded-sm bg-amber-400" /> critical chain
        </span>
        <span>
          {selected
            ? "Showing the selected task's full dependency chain — click it (or the background) to clear."
            : "Hover a task for its direct links · click to trace its full dependency chain."}
        </span>
      </div>
    </div>
  );
}

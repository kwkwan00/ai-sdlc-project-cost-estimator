"use client";

import { useState } from "react";

import { PHASE_COLORS, PHASE_FALLBACK_COLOR } from "@/lib/wbs-colors";
import type { WbsScheduleResult } from "@/lib/wbs-schedule";
import { PHASE_LABELS, type Phase } from "@/lib/types";

/** Week-axis Gantt for the WBS Timeline tab — one swimlane per team member (a role's parallel
 *  people get a lane each). Each task is a bar positioned by its scheduled start/duration, colored
 *  by phase; critical-chain tasks are outlined. Tasks dependent on others start later; independent
 *  tasks on different members overlap, so parallelism is visible across lanes.
 *
 *  The bar scale is LOCKED to a fixed ~10-week window so tasks stay large and readable regardless of
 *  the project length; longer schedules are PAGED through 10 weeks at a time (no horizontal scroll).
 *  A project that fits in one window shows it all (no paging). */
const WINDOW_WEEKS = 10;

export function WbsGanttChart({ schedule }: { schedule: WbsScheduleResult }) {
  const { rows, totalWeeks } = schedule;
  const [page, setPage] = useState(0);

  if (totalWeeks <= 0 || rows.length === 0) {
    return <p className="text-sm muted">No schedule to display for this estimate.</p>;
  }

  const pageCount = Math.max(1, Math.ceil(totalWeeks / WINDOW_WEEKS));
  const paged = pageCount > 1;
  // A single-window project fits to width; a multi-window one locks the scale at WINDOW_WEEKS.
  const windowWeeks = paged ? WINDOW_WEEKS : Math.max(totalWeeks, 0.1);
  const cur = Math.min(Math.max(0, page), pageCount - 1);
  const windowStart = cur * windowWeeks;
  const windowEnd = windowStart + windowWeeks;

  const leftPct = (w: number) =>
    `${Math.min(100, Math.max(0, ((w - windowStart) / windowWeeks) * 100))}%`;
  const widthPct = (a: number, b: number) =>
    `${Math.min(100, Math.max(0, ((b - a) / windowWeeks) * 100))}%`;

  // Integer-week gridlines across the window (locked: every week is the same width).
  const ticks: number[] = [];
  for (let w = Math.round(windowStart); w <= windowEnd + 1e-9; w++) ticks.push(w);

  const labelCol = "w-40 shrink-0";
  const phasesPresent = [
    ...new Set(schedule.tasks.map((t) => t.phase).filter((p): p is Phase => p !== null)),
  ];
  const windowEndLabel = Math.min(windowEnd, totalWeeks);

  return (
    <div className="space-y-2">
      {/* paging toolbar (only when the schedule spans more than one window) */}
      {paged && (
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs muted tabular-nums">
            Weeks {Math.round(windowStart)}–{Math.round(windowEndLabel)} of {Math.ceil(totalWeeks)} ·
            window {cur + 1}/{pageCount}
          </p>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => setPage(cur - 1)}
              disabled={cur === 0}
              className="rounded border border-slate-200 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              ‹ Prev
            </button>
            <button
              type="button"
              onClick={() => setPage(cur + 1)}
              disabled={cur >= pageCount - 1}
              className="rounded border border-slate-200 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next ›
            </button>
          </div>
        </div>
      )}

      <div className="rounded-lg border border-slate-200 p-3">
        {/* week axis */}
        <div className="flex items-end gap-3">
          <div className={labelCol} />
          <div className="relative h-5 flex-1">
            {ticks.map((t) => (
              <span
                key={t}
                className="absolute -translate-x-1/2 text-xs tabular-nums text-slate-400"
                style={{ left: leftPct(t) }}
              >
                {t}w
              </span>
            ))}
          </div>
        </div>

        {/* member swimlanes (one tall row per used member-slot) */}
        <div className="mt-1.5 space-y-1.5">
          {rows.map((row) => {
            // Tasks overlapping the current window (clipped to it).
            const visible = row.tasks.filter(
              (t) => t.endWeek > windowStart - 1e-9 && t.startWeek < windowEnd + 1e-9,
            );
            return (
              <div key={`${row.roleId}#${row.slot}`} className="flex items-center gap-3">
                <div
                  className={`${labelCol} truncate text-sm ${
                    row.firstOfMember ? "font-medium text-slate-700" : "text-slate-400"
                  }`}
                  title={row.memberLabel}
                >
                  {row.firstOfMember ? (
                    row.memberLabel
                  ) : (
                    <span className="pl-2 text-xs">↳ parallel</span>
                  )}
                </div>
                <div className="relative h-11 flex-1 rounded bg-slate-50 ring-1 ring-inset ring-slate-100">
                  {ticks.map((t) => (
                    <span
                      key={t}
                      className="absolute top-0 bottom-0 w-px bg-slate-100"
                      style={{ left: leftPct(t) }}
                    />
                  ))}
                  {visible.map((task) => {
                    const color = task.phase
                      ? PHASE_COLORS[task.phase] ?? PHASE_FALLBACK_COLOR
                      : PHASE_FALLBACK_COLOR;
                    const s = Math.max(task.startWeek, windowStart);
                    const e = Math.min(task.endWeek, windowEnd);
                    const clipL = task.startWeek < windowStart - 1e-9;
                    const clipR = task.endWeek > windowEnd + 1e-9;
                    return (
                      <div
                        key={task.id}
                        className={`absolute top-1 bottom-1 flex items-center overflow-hidden rounded px-2 ${
                          task.isCritical ? "ring-2 ring-amber-500" : ""
                        } ${clipL ? "rounded-l-none" : ""} ${clipR ? "rounded-r-none" : ""}`}
                        style={{
                          left: leftPct(s),
                          width: widthPct(s, e),
                          minWidth: 6, // keep a sub-hour / zero-duration task visible
                          backgroundColor: color,
                        }}
                        title={`${task.name} · ${row.memberLabel} · ${
                          task.phase ? PHASE_LABELS[task.phase] : "—"
                        } · wk ${task.startWeek.toFixed(1)}–${task.endWeek.toFixed(1)} (${Math.round(
                          task.hours,
                        )} h)${task.isCritical ? " · critical chain" : ""}`}
                      >
                        <span className="truncate text-xs font-medium text-white">{task.name}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* legends */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-slate-400">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-3 rounded-sm ring-2 ring-amber-500" /> critical chain
        </span>
        {phasesPresent.map((ph) => (
          <span key={ph} className="flex items-center gap-1">
            <span
              className="inline-block h-2 w-3 rounded-sm"
              style={{ backgroundColor: PHASE_COLORS[ph] ?? PHASE_FALLBACK_COLOR }}
            />
            {PHASE_LABELS[ph]}
          </span>
        ))}
      </div>
    </div>
  );
}

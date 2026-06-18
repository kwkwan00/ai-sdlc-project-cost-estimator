"use client";

import type { Milestone, ScheduleResult } from "@/lib/schedule";

function milestoneColor(kind: Milestone["kind"]): string {
  return kind === "launch" ? "bg-emerald-500" : kind === "kickoff" ? "bg-slate-400" : "bg-brand-500";
}

/** Week-axis Gantt for the Timeline tab. Pure presentation over `deriveSchedule(...)`:
 *  each phase is a bar positioned by its start/duration (as a % of the project span);
 *  critical-path bars are emphasized, off-critical bars show a slack "ghost", and the
 *  milestone strip sits on the same axis. */
function axisTicks(totalWeeks: number): number[] {
  if (totalWeeks <= 0) return [0];
  const target = 6; // ~6 gridlines
  const raw = totalWeeks / target;
  const step = [1, 2, 4, 5, 10, 20, 50].find((s) => s >= raw) ?? Math.ceil(raw);
  const ticks: number[] = [];
  for (let w = 0; w <= totalWeeks + 1e-9; w += step) ticks.push(Math.round(w));
  return ticks;
}

export function GanttChart({ schedule }: { schedule: ScheduleResult }) {
  const { phases, totalWeeks, milestones } = schedule;
  const visible = phases.filter((p) => p.durationWeeks > 0);
  if (totalWeeks <= 0 || visible.length === 0) {
    return <p className="text-sm muted">No schedule to display for this estimate.</p>;
  }
  const pct = (w: number) => `${Math.min(100, Math.max(0, (w / totalWeeks) * 100))}%`;
  const ticks = axisTicks(totalWeeks);
  const labelCol = "w-28 shrink-0";

  return (
    <div className="space-y-2">
      {/* week axis */}
      <div className="flex items-end gap-2">
        <div className={labelCol} />
        <div className="relative flex-1 h-4">
          {ticks.map((t) => (
            <span
              key={t}
              className="absolute -translate-x-1/2 text-[10px] tabular-nums text-slate-400"
              style={{ left: pct(t) }}
            >
              {t}w
            </span>
          ))}
        </div>
      </div>

      {/* phase rows */}
      {visible.map((p) => (
        <div key={p.phase} className="flex items-center gap-2">
          <div className={`${labelCol} text-xs text-slate-600 truncate`} title={p.label}>
            {p.label}
          </div>
          <div className="relative flex-1 h-6 rounded bg-slate-50 ring-1 ring-inset ring-slate-100">
            {/* gridlines */}
            {ticks.map((t) => (
              <span
                key={t}
                className="absolute top-0 bottom-0 w-px bg-slate-100"
                style={{ left: pct(t) }}
              />
            ))}
            {/* slack ghost (latest-finish extension) */}
            {p.slackWeeks > 1e-6 && (
              <div
                className="absolute top-1 bottom-1 rounded-sm border border-dashed border-slate-300"
                style={{ left: pct(p.startWeek), width: pct(p.durationWeeks + p.slackWeeks) }}
                title={`${p.slackWeeks.toFixed(1)} wk slack`}
              />
            )}
            {/* the bar */}
            <div
              className={`absolute top-0.5 bottom-0.5 rounded flex items-center px-1.5 ${
                p.isCritical ? "bg-amber-500" : "bg-brand-400"
              }`}
              style={{ left: pct(p.startWeek), width: pct(p.durationWeeks) }}
              title={`${p.label}: wk ${p.startWeek.toFixed(1)}–${p.endWeek.toFixed(1)} (${p.durationWeeks.toFixed(1)} wk)${
                p.isCritical ? " · critical path" : ` · ${p.slackWeeks.toFixed(1)} wk slack`
              }`}
            >
              <span className="text-[10px] font-medium text-white tabular-nums whitespace-nowrap">
                {p.durationWeeks.toFixed(1)}w
              </span>
            </div>
          </div>
        </div>
      ))}

      {/* milestone markers — numbered on the axis (no free text, so nothing overlaps when
          milestones cluster); the names live in the readable key below, tied by number. */}
      <div className="flex items-center gap-2 pt-1">
        <div className={`${labelCol} text-[10px] uppercase tracking-wide muted`}>Milestones</div>
        <div className="relative flex-1 h-5">
          <span className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-slate-200" />
          {milestones.map((m, i) => (
            <span
              key={`${m.name}-${i}`}
              className={`absolute top-1/2 flex h-4 w-4 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full text-[8px] font-semibold text-white ring-2 ring-white ${milestoneColor(m.kind)}`}
              style={{ left: pct(m.week) }}
              title={`${m.name} · wk ${m.week.toFixed(1)}`}
            >
              {i + 1}
            </span>
          ))}
        </div>
      </div>

      {/* milestone key — fully readable list (number · week · name), aligned under the track */}
      <div className="flex gap-2">
        <div className={labelCol} />
        <ol className="flex flex-1 flex-wrap gap-x-4 gap-y-1 text-[10px] text-slate-500">
          {milestones.map((m, i) => (
            <li key={`${m.name}-${i}`} className="flex items-center gap-1.5">
              <span
                className={`flex h-3.5 w-3.5 items-center justify-center rounded-full text-[7px] font-semibold text-white ${milestoneColor(m.kind)}`}
              >
                {i + 1}
              </span>
              <span className="tabular-nums text-slate-400">wk {Math.round(m.week)}</span>
              <span>{m.name}</span>
            </li>
          ))}
        </ol>
      </div>

      {/* legend */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1 text-[10px] text-slate-400">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-3 rounded-sm bg-amber-500" /> critical path
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-3 rounded-sm bg-brand-400" /> has slack
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-3 rounded-sm border border-dashed border-slate-300" /> slack
        </span>
      </div>
    </div>
  );
}

"use client";

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { formatHours } from "@/lib/format";

export interface EffortShareSlice {
  label: string;
  hours: number;
  color: string;
}

/** Categorical palette for work-package slices (phases use their own phase colors). */
export const EFFORT_PALETTE = [
  "#6366f1",
  "#0ea5e9",
  "#10b981",
  "#f59e0b",
  "#ef4444",
  "#8b5cf6",
  "#ec4899",
  "#14b8a6",
  "#f97316",
  "#84cc16",
  "#06b6d4",
  "#a855f7",
];

/** Donut of effort share — one segment per slice (a phase, or a WBS work package), sized by its
 *  most-likely hours. Center shows the total. Pure presentation over a pre-shaped slice list, so the
 *  caller decides whether to group by phase or by work package. */
export function EffortShareDonut({ data }: { data: EffortShareSlice[] }) {
  const slices = data.filter((d) => d.hours > 0);
  const total = slices.reduce((s, d) => s + d.hours, 0);

  if (slices.length === 0) {
    return <p className="text-sm muted">No effort to display.</p>;
  }

  return (
    <div className="relative h-[26rem] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={slices}
            dataKey="hours"
            nameKey="label"
            innerRadius={88}
            outerRadius={138}
            paddingAngle={2}
            stroke="none"
          >
            {/* Key by index too: WBS work-package labels (`n.name`) aren't unique — the planner can
                emit dupes and users rename freely (only ids are unique) — so `label` alone would
                collide and break React reconciliation on re-render. */}
            {slices.map((d, i) => (
              <Cell key={`${d.label}-${i}`} fill={d.color} />
            ))}
          </Pie>
          <Tooltip
            formatter={(value: number, _name, item) => [
              `${value} h`,
              item?.payload?.label as string,
            ]}
            // Solid background + raised above the center total (a sibling div painted on top of the
            // chart) so the tooltip text stays readable when it overlaps the donut hole.
            wrapperStyle={{ zIndex: 20 }}
            contentStyle={{
              backgroundColor: "#ffffff",
              border: "1px solid #e2e8f0",
              borderRadius: "0.375rem",
              padding: "0.25rem 0.5rem",
              fontSize: "0.75rem",
              boxShadow: "0 1px 3px rgb(0 0 0 / 0.12)",
            }}
            itemStyle={{ color: "#334155" }}
          />
        </PieChart>
      </ResponsiveContainer>
      {/* Center total — sits over the donut hole (no legend, so it's truly centred). Hover a slice
          for its label + hours. */}
      <div className="pointer-events-none absolute inset-x-0 top-1/2 -translate-y-1/2 text-center">
        <p className="text-2xl font-semibold leading-none">{formatHours(total)}</p>
        <p className="mt-0.5 text-[11px] uppercase tracking-wide text-slate-400">total</p>
      </div>
    </div>
  );
}

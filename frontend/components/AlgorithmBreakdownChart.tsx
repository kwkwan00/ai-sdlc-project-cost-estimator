"use client";

import {
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts";

import { algorithmColor, algorithmInfo } from "@/lib/algorithms";
import { formatHours } from "@/lib/format";
import { PHASE_LABELS, type PhaseEstimate } from "@/lib/types";

interface Props {
  phases: PhaseEstimate[];
  mode: "ai_assisted" | "manual_only";
}

/** Donut of effort share by estimation algorithm — one segment per phase,
 *  colored to match its algorithm. Center shows the total most-likely hours. */
export function AlgorithmBreakdownChart({ phases, mode }: Props) {
  const data = phases
    .map((p) => {
      const range =
        mode === "ai_assisted" ? p.ai_assisted_hours : p.manual_only_hours;
      return {
        algorithm: algorithmInfo(p.algorithm)?.name ?? p.algorithm,
        phase: PHASE_LABELS[p.phase],
        hours: Math.round(range.most_likely),
        color: algorithmColor(p.algorithm),
      };
    })
    .filter((d) => d.hours > 0);

  const total = data.reduce((s, d) => s + d.hours, 0);

  return (
    <div className="relative h-[26rem] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={data}
            dataKey="hours"
            nameKey="algorithm"
            innerRadius={88}
            outerRadius={138}
            paddingAngle={2}
            stroke="none"
          >
            {data.map((d) => (
              <Cell key={d.phase} fill={d.color} />
            ))}
          </Pie>
          <Tooltip
            formatter={(value: number, _name, item) => [
              `${value} h`,
              item?.payload?.phase as string,
            ]}
          />
          <Legend
            iconType="circle"
            wrapperStyle={{ fontSize: "0.75rem" }}
          />
        </PieChart>
      </ResponsiveContainer>
      {/* Center total — sits over the donut hole. Top half so the legend below
          doesn't overlap. */}
      <div className="pointer-events-none absolute inset-x-0 top-[42%] -translate-y-1/2 text-center">
        <p className="text-2xl font-semibold leading-none">{formatHours(total)}</p>
        <p className="mt-0.5 text-[11px] uppercase tracking-wide text-slate-400">
          total
        </p>
      </div>
    </div>
  );
}

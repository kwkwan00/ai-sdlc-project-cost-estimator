"use client";

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

import { PHASE_LABELS, type PhaseEstimate } from "@/lib/types";

interface Props {
  phases: PhaseEstimate[];
  mode: "ai_assisted" | "manual_only";
}

export function PhaseBar({ phases, mode }: Props) {
  const data = phases.map((p) => {
    const range = mode === "ai_assisted" ? p.ai_assisted_hours : p.manual_only_hours;
    return {
      name: PHASE_LABELS[p.phase],
      optimistic: Math.round(range.optimistic),
      most_likely: Math.round(range.most_likely - range.optimistic),
      pessimistic: Math.round(range.pessimistic - range.most_likely),
      total: Math.round(range.pessimistic),
    };
  });

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 8, right: 24, left: 24, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis type="number" tickFormatter={(n) => `${n}h`} />
          <YAxis type="category" dataKey="name" width={120} />
          <Tooltip formatter={(value: number) => `${value} h`} />
          <Legend />
          <Bar dataKey="optimistic" stackId="a" fill="#a5b4fc" name="Optimistic" />
          <Bar dataKey="most_likely" stackId="a" fill="#6366f1" name="→ Most likely" />
          <Bar dataKey="pessimistic" stackId="a" fill="#312e81" name="→ Pessimistic" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

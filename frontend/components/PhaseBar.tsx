"use client";

import {
  BarChart,
  Bar,
  ErrorBar,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

import { hasPercentiles } from "@/lib/fan-chart";
import { PHASE_LABELS, type PhaseEstimate } from "@/lib/types";

interface Props {
  phases: PhaseEstimate[];
  mode: "ai_assisted" | "manual_only";
}

export function PhaseBar({ phases, mode }: Props) {
  const data = phases.map((p) => {
    const range = mode === "ai_assisted" ? p.ai_assisted_hours : p.manual_only_hours;
    // P10–P90 whisker, when the range was simulated. ErrorBar offsets are relative
    // to the segment's cumulative value (most_likely), so [mid−p10, p90−mid] draws
    // a band from P10 to P90 around the most-likely tick. Undefined → no whisker.
    const ml = Math.round(range.most_likely);
    const whisker: [number, number] | undefined = hasPercentiles(range)
      ? [ml - range.percentiles.p10, range.percentiles.p90 - ml]
      : undefined;
    return {
      name: PHASE_LABELS[p.phase],
      optimistic: Math.round(range.optimistic),
      most_likely: ml - Math.round(range.optimistic),
      pessimistic: Math.round(range.pessimistic) - ml,
      total: Math.round(range.pessimistic),
      whisker,
    };
  });

  return (
    <div className="h-[26rem] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 8, right: 24, left: 24, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis type="number" tickFormatter={(n) => `${n}h`} />
          <YAxis type="category" dataKey="name" width={120} />
          <Tooltip formatter={(value: number) => `${value} h`} />
          <Legend />
          <Bar dataKey="optimistic" stackId="a" fill="#a5b4fc" name="Optimistic" />
          <Bar dataKey="most_likely" stackId="a" fill="#6366f1" name="→ Most likely">
            {/* P10–P90 whisker around the most-likely tick (simulated ranges only;
                undefined offsets render nothing). */}
            <ErrorBar
              dataKey="whisker"
              direction="x"
              width={4}
              strokeWidth={1.5}
              stroke="#1e1b4b"
            />
          </Bar>
          <Bar dataKey="pessimistic" stackId="a" fill="#312e81" name="→ Pessimistic" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

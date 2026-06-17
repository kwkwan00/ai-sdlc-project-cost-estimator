"use client";

import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatHours } from "@/lib/format";
import { buildTornado } from "@/lib/tornado";
import type { PhaseEstimate } from "@/lib/types";

interface Props {
  phases: PhaseEstimate[];
  mode: "ai_assisted" | "manual_only";
}

/** Tornado / sensitivity chart: one horizontal floating bar per phase, drawn from
 *  the phase's low→high uncertainty bound (P10–P90 when simulated, else the
 *  optimistic→pessimistic spread), ranked top-to-bottom by spread so the widest
 *  driver sits on top — the classic tornado shape. Answers "which phases drive the
 *  project's total uncertainty?".
 *
 *  All ranking/band math lives in `lib/tornado.ts`; this is visual only. The bar is
 *  a ranged `<Bar>` (dataKey returns `[low, high]`, the same recharts trick the fan
 *  chart uses for `<Area>`). Bars fade from deep to light indigo by rank. */
export function TornadoChart({ phases, mode }: Props) {
  const rows = buildTornado(phases, mode);
  const data = rows.map((r) => ({
    label: r.label,
    // Ranged bar: recharts draws a floating bar when the dataKey returns [min, max].
    band: [Math.round(r.low), Math.round(r.high)] as [number, number],
    spread: Math.round(r.spread),
    mid: Math.round(r.mid),
    share: r.share,
    simulated: r.simulated,
  }));

  // Deepest indigo for the top driver, fading down the ranking.
  const shade = (i: number) => {
    const palette = ["#4338ca", "#6366f1", "#818cf8", "#a5b4fc", "#c7d2fe", "#e0e7ff"];
    return palette[Math.min(i, palette.length - 1)];
  };

  // Pad the value axis a touch beyond the widest band so edges aren't clipped.
  const maxHigh = data.reduce((m, d) => Math.max(m, d.band[1]), 0);
  const minLow = data.reduce((m, d) => Math.min(m, d.band[0]), maxHigh);
  const pad = Math.max(1, (maxHigh - minLow) * 0.04);

  return (
    <div className="h-[20rem] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          layout="vertical"
          margin={{ top: 8, right: 28, left: 8, bottom: 8 }}
        >
          <XAxis
            type="number"
            domain={[Math.max(0, minLow - pad), maxHigh + pad]}
            tickFormatter={(n: number) => formatHours(n)}
            tick={{ fontSize: 11 }}
          />
          <YAxis type="category" dataKey="label" width={120} tick={{ fontSize: 11 }} />
          <Tooltip
            cursor={{ fill: "rgba(99,102,241,0.06)" }}
            formatter={(value: number | [number, number]) => {
              if (Array.isArray(value)) {
                const [lo, hi] = value;
                return [
                  `${formatHours(lo)} – ${formatHours(hi)} (±${formatHours((hi - lo) / 2)})`,
                  "Uncertainty band",
                ];
              }
              return [formatHours(value), "Uncertainty band"];
            }}
          />
          <Bar dataKey="band" radius={3} isAnimationActive={false}>
            {data.map((d, i) => (
              <Cell key={d.label} fill={shade(i)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

"use client";

import {
  Area,
  ComposedChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { toFanSeries } from "@/lib/fan-chart";
import { formatHours } from "@/lib/format";
import type { HourRange } from "@/lib/types";

interface Props {
  /** The distribution to visualise (a phase or project-total `HourRange`). */
  range: HourRange;
  /** Category-axis label / series name, e.g. the scenario name. */
  label?: string;
  /** Base hue for the bands and most-likely marker. Defaults to brand indigo. */
  color?: string;
  /** Tailwind height class for the chart host. Defaults to the compact per-phase
   *  size; pass a taller class (e.g. `h-64`) for the prominent project-total chart. */
  heightClass?: string;
}

/** Horizontal Monte Carlo fan chart for a single `HourRange`: a light P5–P95
 *  outer band, a darker P10–P90 inner band, and a dashed reference line at the
 *  deterministic most-likely value. When the range has no simulated percentiles
 *  both bands collapse to the optimistic→pessimistic spread (handled in
 *  `toFanSeries`), so the chart still renders a sensible degenerate band.
 *
 *  Matches the review components: indigo palette, rounded card host, recharts in a
 *  `ResponsiveContainer`. Visual only — all distribution math lives in
 *  `lib/fan-chart.ts`. */
export function FanChart({
  range,
  label = "Hours",
  color = "#6366f1",
  heightClass = "h-[9rem]",
}: Props) {
  const data = toFanSeries(range, label);
  const row = data[0];

  // Pad the value-axis domain ~4% beyond the outer band so the markers/edges
  // aren't clipped at the chart bounds.
  const lo = row.outer[0];
  const hi = row.outer[1];
  const pad = Math.max(1, (hi - lo) * 0.04);

  return (
    <div className={`${heightClass} w-full`}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart
          data={data}
          layout="vertical"
          // Extra top headroom so the most-likely marker's value label (rendered
          // above the reference line) isn't clipped at the chart's top edge.
          margin={{ top: 24, right: 28, left: 8, bottom: 8 }}
        >
          <XAxis
            type="number"
            domain={[lo - pad, hi + pad]}
            tickFormatter={(n: number) => formatHours(n)}
            tick={{ fontSize: 11 }}
          />
          <YAxis type="category" dataKey="label" width={90} tick={{ fontSize: 11 }} />
          <Tooltip
            formatter={(value: number | [number, number], name) => {
              const fmt = (n: number) => formatHours(n);
              if (Array.isArray(value)) {
                return [`${fmt(value[0])} – ${fmt(value[1])}`, name as string];
              }
              return [fmt(value), name as string];
            }}
          />
          {/* Outer P5–P95 band (light). Ranged area: dataKey returns [min, max]. */}
          <Area
            dataKey="outer"
            name={row.simulated ? "P5–P95" : "Optimistic–Pessimistic"}
            stroke="none"
            fill={color}
            fillOpacity={0.18}
            isAnimationActive={false}
          />
          {/* Inner P10–P90 band (darker), only meaningful when simulated. */}
          {row.simulated && (
            <Area
              dataKey="inner"
              name="P10–P90"
              stroke="none"
              fill={color}
              fillOpacity={0.4}
              isAnimationActive={false}
            />
          )}
          {/* Deterministic most-likely marker. */}
          <ReferenceLine
            x={row.mid}
            stroke={color}
            strokeWidth={2}
            strokeDasharray="4 3"
            label={{
              value: formatHours(row.mid),
              position: "top",
              fontSize: 11,
              fill: color,
            }}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

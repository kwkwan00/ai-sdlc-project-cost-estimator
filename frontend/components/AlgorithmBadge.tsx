"use client";

import { AlgorithmTooltip } from "@/components/AlgorithmTooltip";
import { algorithmColor, algorithmInfo } from "@/lib/algorithms";

/** A colored pill identifying the estimation algorithm (color-matched to the
 *  breakdown chart) with its explanatory tooltip. */
export function AlgorithmBadge({ algorithm }: { algorithm: string }) {
  const info = algorithmInfo(algorithm);
  const color = algorithmColor(algorithm);
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
      style={{ backgroundColor: `${color}1a`, color }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: color }}
      />
      {info?.name ?? algorithm}
      <AlgorithmTooltip algorithm={algorithm} />
    </span>
  );
}

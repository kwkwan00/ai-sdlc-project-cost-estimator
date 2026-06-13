import { confidenceLevel } from "@/lib/review-ui";

const FILL: Record<string, string> = {
  low: "bg-rose-400",
  medium: "bg-amber-400",
  high: "bg-emerald-500",
};

/** A small horizontal meter for a 0..1 confidence, colored by level. */
export function ConfidenceMeter({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  return (
    <div className="flex items-center gap-2" title={`Confidence ${pct}%`}>
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-200">
        <div
          className={`h-full rounded-full ${FILL[confidenceLevel(value)]}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs text-slate-500">{pct}%</span>
    </div>
  );
}

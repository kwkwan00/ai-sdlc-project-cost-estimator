import {
  formatMetricValue,
  humanizeKey,
  isHoursMetric,
  toMetrics,
} from "@/lib/breakdown";

interface Props {
  breakdown: Record<string, number>;
  reductionPct: number;
  notes: string;
}

/** Small gauge for the effective AI reduction — green for a saving, rose when AI is
 *  net-slower (the model allows negative reductions). */
function ReductionBar({ pct }: { pct: number }) {
  const negative = pct < 0;
  const width = Math.min(100, Math.abs(pct) * 3); // ~33% reduction fills the bar
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-slate-600">Effective AI reduction</span>
        <span
          className={`font-medium ${
            negative
              ? "text-rose-600"
              : pct > 0
                ? "text-emerald-600"
                : "text-slate-500"
          }`}
        >
          {pct}%{negative ? " (slower)" : ""}
        </span>
      </div>
      <div className="mt-0.5 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full ${negative ? "bg-rose-400" : "bg-emerald-500"}`}
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  );
}

/** Renders a twin's structured `breakdown` graphically: effort components as
 *  magnitude bars, parameters as chips, the AI reduction as a gauge, and the prose
 *  reasoning below. Falls back to plain prose when there's no breakdown (stubs). */
export function BreakdownView({ breakdown, reductionPct, notes }: Props) {
  const metrics = toMetrics(breakdown);
  const hours = metrics.filter((m) => isHoursMetric(m.key));
  const params = metrics.filter((m) => !isHoursMetric(m.key));
  const maxHours = Math.max(1, ...hours.map((m) => m.value));

  if (metrics.length === 0) {
    return notes ? (
      <p className="text-xs muted italic break-words">{notes}</p>
    ) : null;
  }

  return (
    <div className="rounded-md border border-slate-100 bg-slate-50/60 p-3 space-y-3">
      <p className="text-[10px] uppercase tracking-wide muted">Method breakdown</p>

      {hours.length > 0 && (
        <div className="space-y-1.5">
          {hours.map((m) => (
            <div key={m.key}>
              <div className="flex justify-between text-xs">
                <span className="text-slate-600">{humanizeKey(m.key)}</span>
                <span className="font-medium">
                  {formatMetricValue(m.key, m.value)}
                </span>
              </div>
              <div className="mt-0.5 h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
                <div
                  className="h-full rounded-full bg-brand-500"
                  style={{ width: `${(m.value / maxHours) * 100}%` }}
                />
              </div>
            </div>
          ))}
        </div>
      )}

      {params.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {params.map((m) => (
            <span
              key={m.key}
              className="inline-flex items-center gap-1 rounded-md bg-white px-2 py-1 text-[11px] ring-1 ring-slate-200"
            >
              <span className="text-slate-500">{humanizeKey(m.key)}</span>
              <span className="font-semibold text-slate-700">
                {formatMetricValue(m.key, m.value)}
              </span>
            </span>
          ))}
        </div>
      )}

      <ReductionBar pct={reductionPct} />

      {notes && <p className="text-xs muted italic break-words">{notes}</p>}
    </div>
  );
}

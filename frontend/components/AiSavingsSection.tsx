import { formatHours, formatUSD } from "@/lib/format";
import { reconciledTotals, sharePct } from "@/lib/review-ui";
import { PHASE_LABELS, type DualScenarioEstimate } from "@/lib/types";

/** Explains where the AI-assistance savings come from: an overall headline plus a
 *  per-phase comparison of AI-assisted vs manual hours, with the saved (or, when AI
 *  is net-slower, the added) portion highlighted. Independent of the AI/manual
 *  toggle — it always contrasts the two scenarios. */
export function AiSavingsSection({ fe }: { fe: DualScenarioEstimate }) {
  const phases = fe.phases;
  // Same reconciled, rounded totals the top summary uses, so the headline matches.
  const { aiHours, manualHours, savedHours, savedCost } = reconciledTotals({
    aiHours: fe.total_ai_assisted_hours.most_likely,
    manualHours: fe.total_manual_only_hours.most_likely,
    aiCost: fe.total_cost_ai_assisted_usd,
    manualCost: fe.total_cost_manual_only_usd,
  });
  const savedPct = sharePct(savedHours, manualHours);
  const scaleMax = Math.max(
    1,
    ...phases.map((p) =>
      Math.max(p.manual_only_hours.most_likely, p.ai_assisted_hours.most_likely),
    ),
  );
  const anySlower = phases.some((p) => p.effective_ai_reduction_pct < 0);

  return (
    <section className="card space-y-4">
      <h2 className="section-title">AI assistance savings</h2>
      <p className="text-xs muted">
        With the team&apos;s AI tooling this project is projected at{" "}
        <span className="font-semibold text-slate-700">{formatHours(aiHours)}</span>{" "}
        vs{" "}
        <span className="font-semibold text-slate-700">
          {formatHours(manualHours)}
        </span>{" "}
        manual — a saving of{" "}
        <span className="font-semibold text-emerald-600">
          {formatHours(savedHours)} ({savedPct}%)
        </span>
        , about{" "}
        <span className="font-semibold text-emerald-600">
          {formatUSD(savedCost)}
        </span>
        . Each phase&apos;s reduction is derived from its AI tooling level, codebase
        familiarity, and team seniority
        {anySlower
          ? " — where verification overhead outweighs the help, AI is net-slower (shown in rose)."
          : "."}
      </p>

      <div className="space-y-2.5">
        {phases.map((p) => {
          const manual = p.manual_only_hours.most_likely;
          const ai = p.ai_assisted_hours.most_likely;
          const saved = manual - ai;
          const slower = saved < 0;
          const baseW = (Math.min(ai, manual) / scaleMax) * 100;
          const deltaW = (Math.abs(saved) / scaleMax) * 100;
          return (
            <div key={p.phase}>
              <div className="flex items-baseline justify-between text-xs">
                <span className="font-medium text-slate-700">
                  {PHASE_LABELS[p.phase]}
                </span>
                <span className={slower ? "text-rose-600" : "text-emerald-600"}>
                  {slower
                    ? `+${formatHours(-saved)} slower`
                    : `−${formatHours(saved)} saved`}{" "}
                  <span className="text-slate-400">
                    ({p.effective_ai_reduction_pct}%)
                  </span>
                </span>
              </div>
              <div className="mt-1 flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
                <div
                  className="h-full bg-brand-500"
                  style={{ width: `${baseW}%` }}
                  title={slower ? `Manual ${formatHours(manual)}` : `AI-assisted ${formatHours(ai)}`}
                />
                <div
                  className={`h-full ${slower ? "bg-rose-400" : "bg-emerald-400"}`}
                  style={{ width: `${deltaW}%` }}
                  title={
                    slower
                      ? `Added ${formatHours(-saved)}`
                      : `Saved ${formatHours(saved)}`
                  }
                />
              </div>
              <div className="mt-0.5 flex justify-between text-[10px] muted">
                <span>AI {formatHours(ai)}</span>
                <span>Manual {formatHours(manual)}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-3 text-[10px] muted">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm bg-brand-500" />
          AI-assisted hours
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm bg-emerald-400" />
          Hours saved
        </span>
        {anySlower && (
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-sm bg-rose-400" />
            Added (AI slower)
          </span>
        )}
      </div>
    </section>
  );
}

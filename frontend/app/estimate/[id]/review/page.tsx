"use client";

import { useQuery } from "@tanstack/react-query";
import { use, useState } from "react";

import { DualScenarioToggle } from "@/components/DualScenarioToggle";
import { PhaseBar } from "@/components/PhaseBar";
import { StageProgress } from "@/components/StageProgress";
import { getEstimate } from "@/lib/api-client";
import { formatHours, formatPct, formatUSD } from "@/lib/format";
import { ROLE_CATEGORY_LABELS, ROLE_SENIORITY_LABELS } from "@/lib/schemas";
import { PHASE_LABELS } from "@/lib/types";

interface PageProps {
  params: Promise<{ id: string }>;
}

export default function ReviewPage({ params }: PageProps) {
  const { id } = use(params);
  const [mode, setMode] = useState<"ai_assisted" | "manual_only">("ai_assisted");
  const { data, isLoading, error } = useQuery({
    queryKey: ["estimate", id],
    queryFn: () => getEstimate(id),
    refetchInterval: (q) =>
      q.state.data?.status === "completed" || q.state.data?.status === "failed" ? false : 1500,
  });

  if (isLoading || !data) return <div className="card">Loading...</div>;
  if (error)
    return (
      <div className="card text-rose-700">
        Failed: {(error as Error).message}
      </div>
    );

  if (data.status !== "completed" || !data.final_estimate) {
    return (
      <div className="space-y-6 max-w-3xl">
        <StageProgress current={5} />
        <div className="card">Status: {data.status}</div>
      </div>
    );
  }

  const fe = data.final_estimate;
  const totalRange =
    mode === "ai_assisted" ? fe.total_ai_assisted_hours : fe.total_manual_only_hours;
  const totalCost =
    mode === "ai_assisted"
      ? fe.total_cost_ai_assisted_usd
      : fe.total_cost_manual_only_usd;

  return (
    <div className="space-y-6">
      <StageProgress current={5} />

      <header className="flex items-end justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">{data.project_name}</h1>
          <p className="muted">Final estimate · confidence {formatPct(fe.confidence)}</p>
        </div>
        <DualScenarioToggle value={mode} onChange={setMode} />
      </header>

      <section className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div className="card">
          <p className="text-xs muted">Total hours</p>
          <p className="text-2xl font-semibold mt-1">
            {formatHours(totalRange.most_likely)}
          </p>
          <p className="text-xs muted mt-1">
            {formatHours(totalRange.optimistic)} – {formatHours(totalRange.pessimistic)}
          </p>
        </div>
        <div className="card">
          <p className="text-xs muted">Total cost</p>
          <p className="text-2xl font-semibold mt-1">{formatUSD(totalCost)}</p>
        </div>
        <div className="card">
          <p className="text-xs muted">Duration</p>
          <p className="text-2xl font-semibold mt-1">
            {Math.round(fe.duration_weeks_low)}-{Math.round(fe.duration_weeks_high)} wk
          </p>
        </div>
        <div className="card">
          <p className="text-xs muted">AI savings</p>
          <p className="text-2xl font-semibold mt-1">
            {formatHours(fe.ai_hours_saved_pert)}
          </p>
          <p className="text-xs muted mt-1">{formatUSD(fe.ai_cost_saved_usd)}</p>
        </div>
      </section>

      <section className="card space-y-4">
        <h2 className="section-title">Per-phase breakdown</h2>
        <PhaseBar phases={fe.phases} mode={mode} />
        <table className="min-w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase muted">
              <th className="py-2">Phase</th>
              <th className="py-2">Algorithm</th>
              <th className="py-2">Low</th>
              <th className="py-2">Mid</th>
              <th className="py-2">High</th>
              <th className="py-2">Confidence</th>
            </tr>
          </thead>
          <tbody>
            {fe.phases.map((p) => {
              const r = mode === "ai_assisted" ? p.ai_assisted_hours : p.manual_only_hours;
              return (
                <tr key={p.phase} className="border-t border-slate-100">
                  <td className="py-2 font-medium">{PHASE_LABELS[p.phase]}</td>
                  <td className="py-2 text-slate-500">{p.algorithm}</td>
                  <td className="py-2">{formatHours(r.optimistic)}</td>
                  <td className="py-2 font-semibold">{formatHours(r.most_likely)}</td>
                  <td className="py-2">{formatHours(r.pessimistic)}</td>
                  <td className="py-2 text-slate-500">{formatPct(p.confidence)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="card space-y-3">
        <h2 className="section-title">Recommended staffing</h2>
        {fe.headcount_by_role.length === 0 ? (
          <p className="text-sm muted">No roster supplied; staffing not computed.</p>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {fe.headcount_by_role.map((row) => (
              <div key={row.role_id} className="space-y-1">
                <p className="text-xs muted break-words leading-snug line-clamp-3">
                  {row.role_description}
                </p>
                <p className="text-[10px] uppercase tracking-wide text-slate-400">
                  {ROLE_CATEGORY_LABELS[row.category]} /{" "}
                  {ROLE_SENIORITY_LABELS[row.seniority]}
                </p>
                <p className="text-xl font-semibold">{row.headcount}</p>
              </div>
            ))}
          </div>
        )}
        <p className="text-xs muted">
          Weekly burn: {formatUSD(fe.weekly_burn_rate_usd)}
        </p>
      </section>

      <section className="card space-y-3">
        <h2 className="section-title">Assumptions &amp; risks</h2>
        {fe.phases.map((p) => (
          <details key={p.phase} className="border-t border-slate-100 pt-2">
            <summary className="cursor-pointer text-sm font-medium">
              {PHASE_LABELS[p.phase]} ({p.algorithm})
            </summary>
            <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
              <div>
                <p className="text-xs uppercase muted mb-1">Assumptions</p>
                <ul className="list-disc pl-5 space-y-1">
                  {p.assumptions.map((a, i) => (
                    <li key={i}>{a.text}</li>
                  ))}
                </ul>
              </div>
              <div>
                <p className="text-xs uppercase muted mb-1">Risks</p>
                <ul className="list-disc pl-5 space-y-1">
                  {p.risks.map((r, i) => (
                    <li key={i}>{r.description}</li>
                  ))}
                </ul>
              </div>
            </div>
            {p.notes && (
              <p className="text-xs muted mt-2 italic break-words">{p.notes}</p>
            )}
          </details>
        ))}
      </section>
    </div>
  );
}

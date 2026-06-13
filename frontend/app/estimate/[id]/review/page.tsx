"use client";

import { useQuery } from "@tanstack/react-query";
import { use, useState } from "react";

import { AiSavingsSection } from "@/components/AiSavingsSection";
import { AlgorithmBadge } from "@/components/AlgorithmBadge";
import { AlgorithmBreakdownChart } from "@/components/AlgorithmBreakdownChart";
import { BreakdownView } from "@/components/BreakdownView";
import { ConfidenceMeter } from "@/components/ConfidenceMeter";
import { DualScenarioToggle } from "@/components/DualScenarioToggle";
import { Modal } from "@/components/Modal";
import { PhaseBar } from "@/components/PhaseBar";
import { StageProgress } from "@/components/StageProgress";
import { getEstimate } from "@/lib/api-client";
import {
  formatHours,
  formatPct,
  formatTokens,
  formatUSD,
  formatUSDPrecise,
} from "@/lib/format";
import { ROLE_CATEGORY_LABELS, ROLE_SENIORITY_LABELS } from "@/lib/schemas";
import { algorithmColor } from "@/lib/algorithms";
import { reconciledTotals, sharePct } from "@/lib/review-ui";
import { PHASE_LABELS } from "@/lib/types";

interface PageProps {
  params: Promise<{ id: string }>;
}

export default function ReviewPage({ params }: PageProps) {
  const { id } = use(params);
  const [mode, setMode] = useState<"ai_assisted" | "manual_only">("ai_assisted");
  const [openPhase, setOpenPhase] = useState<number | null>(null);
  const [showLlmUsage, setShowLlmUsage] = useState(false);
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
  // Rounded, reconciling totals: AI + saved === manual (hours and cost) exactly,
  // so the top summary cards always add up regardless of mode.
  const totals = reconciledTotals({
    aiHours: fe.total_ai_assisted_hours.most_likely,
    manualHours: fe.total_manual_only_hours.most_likely,
    aiCost: fe.total_cost_ai_assisted_usd,
    manualCost: fe.total_cost_manual_only_usd,
  });
  const totalHours = mode === "ai_assisted" ? totals.aiHours : totals.manualHours;
  const totalCost = mode === "ai_assisted" ? totals.aiCost : totals.manualCost;
  // Sum of per-phase most-likely hours (for each phase's "share of effort" bar).
  const phaseHoursTotal = fe.phases.reduce(
    (sum, p) =>
      sum +
      (mode === "ai_assisted"
        ? p.ai_assisted_hours.most_likely
        : p.manual_only_hours.most_likely),
    0,
  );
  const phaseModal = openPhase !== null ? fe.phases[openPhase] : null;

  return (
    <div className="space-y-6">
      <StageProgress current={5} />

      <header className="flex items-end justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">{data.project_name}</h1>
          <p className="muted">Final estimate · confidence {formatPct(fe.confidence)}</p>
        </div>
        <div className="flex items-center gap-2">
          {fe.llm_usage && fe.llm_usage.call_count > 0 && (
            <button
              type="button"
              onClick={() => setShowLlmUsage(true)}
              aria-label="Estimation LLM cost & usage"
              title="Estimation LLM cost & usage"
              className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-slate-300 text-slate-500 hover:bg-slate-50 hover:text-slate-700 focus:outline-none focus:ring-2 focus:ring-brand-400"
            >
              <svg
                viewBox="0 0 24 24"
                className="h-5 w-5"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <circle cx="12" cy="12" r="9" />
                <path d="M14.5 9.3a2.4 2.4 0 0 0-2.5-1.3c-1.5 0-2.5.8-2.5 1.9 0 1 .8 1.6 2.5 2 1.7.3 2.5 1 2.5 2.1 0 1.1-1 1.9-2.5 1.9a2.4 2.4 0 0 1-2.5-1.3" />
                <path d="M12 6.3v1.7M12 16v1.7" />
              </svg>
            </button>
          )}
          <DualScenarioToggle value={mode} onChange={setMode} />
        </div>
      </header>

      <section className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div className="card">
          <p className="text-xs muted">Total hours</p>
          <p className="text-2xl font-semibold mt-1">{formatHours(totalHours)}</p>
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
            {formatHours(totals.savedHours)}
          </p>
          <p className="text-xs muted mt-1">{formatUSD(totals.savedCost)}</p>
        </div>
      </section>

      <section className="card space-y-4">
        <h2 className="section-title">Per-phase breakdown</h2>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div>
            <p className="text-xs uppercase tracking-wide muted mb-1">
              Hours range per phase
            </p>
            <PhaseBar phases={fe.phases} mode={mode} />
          </div>
          <div>
            <p className="text-xs uppercase tracking-wide muted mb-1">
              Effort share by algorithm
            </p>
            <AlgorithmBreakdownChart phases={fe.phases} mode={mode} />
          </div>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {fe.phases.map((p, idx) => {
            const r =
              mode === "ai_assisted" ? p.ai_assisted_hours : p.manual_only_hours;
            const share = sharePct(r.most_likely, phaseHoursTotal);
            const color = algorithmColor(p.algorithm);
            return (
              <div
                key={p.phase}
                className="rounded-lg border border-slate-200 p-3 space-y-2"
                style={{ borderLeft: `3px solid ${color}` }}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-sm font-medium leading-snug">
                    {PHASE_LABELS[p.phase]}
                  </span>
                  <AlgorithmBadge algorithm={p.algorithm} />
                </div>
                <div>
                  <p className="text-xl font-semibold">
                    {formatHours(r.most_likely)}
                  </p>
                  <p className="text-xs muted">
                    {formatHours(r.optimistic)} – {formatHours(r.pessimistic)}
                  </p>
                </div>
                <div>
                  <div className="flex justify-between text-[10px] muted">
                    <span>Share of effort</span>
                    <span>{share}%</span>
                  </div>
                  <div className="mt-0.5 h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
                    <div
                      className="h-full rounded-full"
                      style={{ width: `${share}%`, backgroundColor: color }}
                    />
                  </div>
                </div>
                <ConfidenceMeter value={p.confidence} />
                <button
                  type="button"
                  onClick={() => setOpenPhase(idx)}
                  className="mt-1 inline-flex items-center gap-1 text-xs font-medium text-brand-600 hover:text-brand-700"
                >
                  Assumptions &amp; risks
                  <span className="text-slate-400">
                    ({p.assumptions.length} · {p.risks.length})
                  </span>
                  <span aria-hidden="true">→</span>
                </button>
              </div>
            );
          })}
        </div>
      </section>

      <AiSavingsSection fe={fe} />

      <section className="card space-y-3">
        <h2 className="section-title">Staffing &amp; cost per role</h2>
        <p className="text-xs muted">
          Hours and labor cost per role for the{" "}
          {mode === "ai_assisted" ? "AI-assisted" : "manual-only"} scenario.
        </p>
        {fe.headcount_by_role.length === 0 ? (
          <p className="text-sm muted">No roster supplied; staffing not computed.</p>
        ) : (
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase muted">
                <th className="py-2">Role</th>
                <th className="py-2">Heads</th>
                <th className="py-2">Hours</th>
                <th className="py-2">Rate</th>
                <th className="py-2">Cost</th>
              </tr>
            </thead>
            <tbody>
              {fe.headcount_by_role.map((row) => {
                const hours =
                  mode === "ai_assisted"
                    ? row.ai_assisted_hours
                    : row.manual_only_hours;
                const cost =
                  mode === "ai_assisted"
                    ? row.ai_assisted_cost_usd
                    : row.manual_only_cost_usd;
                return (
                  <tr key={row.role_id} className="border-t border-slate-100 align-top">
                    <td className="py-2">
                      <div className="font-medium">{row.role_description}</div>
                      <div className="text-[10px] uppercase tracking-wide text-slate-400">
                        {ROLE_CATEGORY_LABELS[row.category]} /{" "}
                        {ROLE_SENIORITY_LABELS[row.seniority]}
                      </div>
                    </td>
                    <td className="py-2">{row.headcount}</td>
                    <td className="py-2">{formatHours(hours)}</td>
                    <td className="py-2 text-slate-500">
                      {formatUSD(row.rate_per_hour)}/h
                    </td>
                    <td className="py-2 font-semibold">{formatUSD(cost)}</td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr className="border-t border-slate-200 font-semibold">
                <td className="py-2">Total</td>
                <td className="py-2">
                  {fe.headcount_by_role.reduce((s, r) => s + r.headcount, 0)}
                </td>
                <td className="py-2" />
                <td className="py-2" />
                <td className="py-2">{formatUSD(totalCost)}</td>
              </tr>
            </tfoot>
          </table>
        )}
        <p className="text-xs muted">
          Weekly burn: {formatUSD(fe.weekly_burn_rate_usd)}
        </p>
      </section>

      {fe.llm_usage && fe.llm_usage.call_count > 0 && (
        <Modal
          open={showLlmUsage}
          onClose={() => setShowLlmUsage(false)}
          title="Estimation LLM cost & usage"
        >
          <p className="text-xs muted mb-3">
            What it cost to <em>produce</em> this estimate via the Anthropic API —
            separate from the project labor cost.
          </p>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div>
              <p className="text-xs muted">API cost</p>
              <p className="text-2xl font-semibold mt-1">
                {formatUSDPrecise(fe.llm_usage.cost_usd)}
              </p>
            </div>
            <div>
              <p className="text-xs muted">LLM calls</p>
              <p className="text-2xl font-semibold mt-1">
                {fe.llm_usage.call_count}
              </p>
            </div>
            <div>
              <p className="text-xs muted">Input tokens</p>
              <p className="text-2xl font-semibold mt-1">
                {formatTokens(fe.llm_usage.input_tokens)}
              </p>
            </div>
            <div>
              <p className="text-xs muted">Output tokens</p>
              <p className="text-2xl font-semibold mt-1">
                {formatTokens(fe.llm_usage.output_tokens)}
              </p>
            </div>
          </div>
          {fe.llm_usage.by_model.length > 0 && (
            <table className="mt-4 min-w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase muted">
                  <th className="py-2">Model</th>
                  <th className="py-2">Calls</th>
                  <th className="py-2">Input</th>
                  <th className="py-2">Output</th>
                  <th className="py-2">Cost</th>
                </tr>
              </thead>
              <tbody>
                {fe.llm_usage.by_model.map((m) => (
                  <tr key={m.model} className="border-t border-slate-100">
                    <td className="py-2 font-medium">{m.model}</td>
                    <td className="py-2">{m.calls}</td>
                    <td className="py-2">{formatTokens(m.input_tokens)}</td>
                    <td className="py-2">{formatTokens(m.output_tokens)}</td>
                    <td className="py-2 font-semibold">
                      {formatUSDPrecise(m.cost_usd)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Modal>
      )}

      {phaseModal && (
        <Modal
          open
          onClose={() => setOpenPhase(null)}
          title={PHASE_LABELS[phaseModal.phase]}
        >
          <div className="space-y-4">
            <AlgorithmBadge algorithm={phaseModal.algorithm} />
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
              <div>
                <p className="text-xs uppercase muted mb-1">
                  Assumptions ({phaseModal.assumptions.length})
                </p>
                {phaseModal.assumptions.length === 0 ? (
                  <p className="text-xs muted">None noted.</p>
                ) : (
                  <ul className="list-disc pl-5 space-y-1">
                    {phaseModal.assumptions.map((a, i) => (
                      <li key={i}>{a.text}</li>
                    ))}
                  </ul>
                )}
              </div>
              <div>
                <p className="text-xs uppercase muted mb-1">
                  Risks ({phaseModal.risks.length})
                </p>
                {phaseModal.risks.length === 0 ? (
                  <p className="text-xs muted">None noted.</p>
                ) : (
                  <ul className="list-disc pl-5 space-y-1">
                    {phaseModal.risks.map((r, i) => (
                      <li key={i}>{r.description}</li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
            <BreakdownView
              breakdown={phaseModal.breakdown}
              reductionPct={phaseModal.effective_ai_reduction_pct}
              notes={phaseModal.notes}
            />
          </div>
        </Modal>
      )}
    </div>
  );
}

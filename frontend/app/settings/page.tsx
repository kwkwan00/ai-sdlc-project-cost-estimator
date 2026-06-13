"use client";

import { useEffect, useState } from "react";

import {
  getReductionBands,
  saveReductionBands,
  type ReductionBandRow,
} from "@/lib/api-client";
import { PHASE_LABELS, type Phase } from "@/lib/types";

const PHASE_ORDER = [
  "discovery",
  "ux_design",
  "development",
  "code_review",
  "deployment",
  "qa_testing",
] as const;

const TOOLING_LABEL: Record<string, string> = {
  autocomplete: "Autocomplete",
  chat: "Chat",
  agentic: "Agentic",
};

function clampPct(raw: string): number {
  const n = Number(raw);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(100, n));
}

export default function SettingsPage() {
  const [rows, setRows] = useState<ReductionBandRow[]>([]);
  const [editable, setEditable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    getReductionBands()
      .then((r) => {
        setRows(r.bands);
        setEditable(r.editable);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  const updateRow = (idx: number, patch: Partial<ReductionBandRow>) => {
    setRows((rs) => rs.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
    setSaved(false);
  };

  const differsFromDefaults = rows.some(
    (r) => r.min_pct !== r.default_min_pct || r.max_pct !== r.default_max_pct,
  );
  const hasInvalid = rows.some((r) => r.min_pct > r.max_pct);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const resp = await saveReductionBands(
        rows.map((r) => ({
          phase: r.phase,
          tooling_level: r.tooling_level,
          min_pct: r.min_pct,
          max_pct: r.max_pct,
        })),
      );
      setRows(resp.bands);
      setEditable(resp.editable);
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const resetToDefaults = () => {
    setRows((rs) =>
      rs.map((r) => ({
        ...r,
        min_pct: r.default_min_pct,
        max_pct: r.default_max_pct,
      })),
    );
    setSaved(false);
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <header className="space-y-2">
        <h1 className="text-2xl font-bold text-slate-900">Settings</h1>
        <p className="muted">
          AI-assistance reduction bands. Each phase&apos;s tooling level has a{" "}
          <span className="font-medium">min–max</span> guardrail; the twin&apos;s
          proposed reduction is clamped into it, then moderated by codebase context
          and team seniority. Tune these to retune the estimator without a redeploy.
        </p>
      </header>

      {!editable && !loading && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
          Postgres isn&apos;t connected, so these are the in-code defaults shown
          <span className="font-medium"> read-only</span>. Connect Postgres
          (set <code>POSTGRES_PASSWORD</code>/<code>POSTGRES_DSN</code>) to persist
          edits.
        </div>
      )}

      {error && (
        <div className="rounded-md border border-rose-300 bg-rose-50 p-3 text-sm text-rose-700">
          {error}
        </div>
      )}

      {loading ? (
        <div className="card">Loading…</div>
      ) : (
        <>
          {PHASE_ORDER.map((phase) => {
            const phaseRows = rows
              .map((r, i) => ({ r, i }))
              .filter(({ r }) => r.phase === phase);
            if (phaseRows.length === 0) return null;
            return (
              <section key={phase} className="card space-y-2">
                <h2 className="section-title">
                  {PHASE_LABELS[phase as Phase]}
                </h2>
                <table className="min-w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase muted">
                      <th className="py-1">Tooling</th>
                      <th className="py-1 w-24">Min %</th>
                      <th className="py-1 w-24">Max %</th>
                      <th className="py-1">Default</th>
                    </tr>
                  </thead>
                  <tbody>
                    {phaseRows.map(({ r, i }) => {
                      const invalid = r.min_pct > r.max_pct;
                      return (
                        <tr key={r.tooling_level} className="border-t border-slate-100">
                          <td className="py-1.5 font-medium">
                            {TOOLING_LABEL[r.tooling_level] ?? r.tooling_level}
                            {r.is_override && (
                              <span className="ml-1 text-[10px] uppercase tracking-wide text-brand-600">
                                edited
                              </span>
                            )}
                          </td>
                          <td className="py-1.5">
                            <input
                              type="number"
                              min={0}
                              max={100}
                              step={0.5}
                              disabled={!editable}
                              value={r.min_pct}
                              onChange={(e) =>
                                updateRow(i, { min_pct: clampPct(e.target.value) })
                              }
                              className={`input py-1 ${invalid ? "border-rose-400" : ""} disabled:opacity-60`}
                              aria-label={`${phase} ${r.tooling_level} min percent`}
                            />
                          </td>
                          <td className="py-1.5">
                            <input
                              type="number"
                              min={0}
                              max={100}
                              step={0.5}
                              disabled={!editable}
                              value={r.max_pct}
                              onChange={(e) =>
                                updateRow(i, { max_pct: clampPct(e.target.value) })
                              }
                              className={`input py-1 ${invalid ? "border-rose-400" : ""} disabled:opacity-60`}
                              aria-label={`${phase} ${r.tooling_level} max percent`}
                            />
                          </td>
                          <td className="py-1.5 text-xs muted">
                            {r.default_min_pct}–{r.default_max_pct}%
                            {invalid && (
                              <span className="ml-2 text-rose-600">min &gt; max</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </section>
            );
          })}

          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs muted" role="status" aria-live="polite">
              {hasInvalid
                ? "Fix the rows where min > max before saving."
                : saved
                  ? "Saved."
                  : ""}
            </p>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={resetToDefaults}
                disabled={!editable || !differsFromDefaults}
                className="btn-secondary text-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Reset to defaults
              </button>
              <button
                type="button"
                onClick={save}
                disabled={!editable || hasInvalid || saving}
                className="btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {saving ? "Saving…" : "Save changes"}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

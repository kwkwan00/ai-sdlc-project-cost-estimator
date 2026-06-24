"use client";

import { useEffect, useRef, useState } from "react";

import {
  getContingency,
  getDefaultRates,
  getDevelopmentSizingMethod,
  getDiscoverySizingMethod,
  getQaSizingMethod,
  getReductionBands,
  getStaffingCoefficients,
  saveContingency,
  saveDefaultRates,
  saveDevelopmentSizingMethod,
  saveDiscoverySizingMethod,
  saveQaSizingMethod,
  saveReductionBands,
  saveStaffingCoefficients,
  type CustomRoleRow,
  type RateRow,
  type ReductionBandRow,
  type SizingMethodResponse,
  type StaffingCoefficientRow,
} from "@/lib/api-client";
import { Tabs, type TabItem } from "@/components/Tabs";
import {
  ROLE_CATEGORY_LABELS,
  ROLE_CATEGORY_OPTIONS,
  ROLE_SENIORITY_LABELS,
  ROLE_SENIORITY_OPTIONS,
} from "@/lib/schemas";
import { PHASE_LABELS, type Phase } from "@/lib/types";
import {
  COEFF_META,
  DEV_SIZING_LABELS,
  DISCOVERY_SIZING_LABELS,
  QA_SIZING_LABELS,
} from "@/lib/settings-content";

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
  const tabs: TabItem[] = [
    {
      id: "methods",
      label: "Estimation methods",
      content: (
        <>
          <DiscoverySizingSection />
          <DevelopmentSizingSection />
          <QaSizingSection />
        </>
      ),
    },
    { id: "ai-reduction", label: "AI reduction", content: <ReductionBandsSection /> },
    { id: "team-scaling", label: "Team scaling", content: <StaffingCoefficientsSection /> },
    {
      id: "cost",
      label: "Cost & contingency",
      content: (
        <>
          <DefaultRatesSection />
          <ContingencySection />
        </>
      ),
    },
  ];

  return (
    <div className="space-y-6 max-w-3xl">
      <header className="space-y-2">
        <h1 className="text-2xl font-bold text-slate-900">Settings</h1>
        <p className="muted">
          Tune the estimator without a redeploy. Edits persist when Postgres is connected;
          otherwise each section shows the in-code defaults read-only.
        </p>
      </header>
      <Tabs tabs={tabs} ariaLabel="Settings sections" />
    </div>
  );
}

function ReductionBandsSection() {
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
    <div className="space-y-6">
      <p className="muted">
        AI-assistance reduction bands. Each phase&apos;s tooling level has a{" "}
        <span className="font-medium">min–max</span> guardrail; the twin&apos;s proposed
        reduction is clamped into it, then moderated by codebase context and team seniority.
      </p>

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

/** Reusable single-choice "sizing method" editor backed by a GET/PUT admin pair. */
function SizingMethodSection({
  title,
  description,
  radioName,
  labels,
  load,
  save: persist,
}: {
  title: string;
  description: string;
  radioName: string;
  labels: Record<string, { label: string; hint: string }>;
  load: () => Promise<SizingMethodResponse>;
  save: (method: string) => Promise<SizingMethodResponse>;
}) {
  const [method, setMethod] = useState<string>("");
  const [methods, setMethods] = useState<string[]>([]);
  const [defaultMethod, setDefaultMethod] = useState<string>("");
  const [editable, setEditable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    load()
      .then((r) => {
        setMethod(r.method);
        setMethods(r.methods);
        setDefaultMethod(r.default_method);
        setEditable(r.editable);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [load]);

  const save = async (next: string) => {
    setMethod(next);
    setSaved(false);
    setSaving(true);
    setError(null);
    try {
      const resp = await persist(next);
      setMethod(resp.method);
      setEditable(resp.editable);
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (loading) return null;

  return (
    <section className="card space-y-3">
      <div>
        <h2 className="section-title">{title}</h2>
        <p className="text-xs muted">{description}</p>
      </div>

      {!editable && (
        <p className="text-xs text-amber-700">
          Postgres isn&apos;t connected — this shows the in-code default ({defaultMethod}),
          read-only.
        </p>
      )}
      {error && <p className="text-sm text-rose-600">{error}</p>}

      <div className="space-y-2">
        {methods.map((m) => {
          const meta = labels[m] ?? { label: m, hint: "" };
          return (
            <label
              key={m}
              className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 ${
                method === m ? "border-brand-300 bg-brand-50/40" : "border-slate-200"
              } ${!editable || saving ? "opacity-60" : ""}`}
            >
              <input
                type="radio"
                name={radioName}
                className="mt-1"
                value={m}
                checked={method === m}
                disabled={!editable || saving}
                onChange={() => save(m)}
              />
              <span>
                <span className="text-sm font-medium">
                  {meta.label}
                  {m === defaultMethod ? " (default)" : ""}
                </span>
                <span className="block text-xs muted">{meta.hint}</span>
              </span>
            </label>
          );
        })}
      </div>

      <p className="text-xs muted" role="status" aria-live="polite">
        {saving ? "Saving…" : saved ? "Saved." : ""}
      </p>
    </section>
  );
}

function DiscoverySizingSection() {
  return (
    <SizingMethodSection
      title="Discovery estimation method"
      description="The sizing algorithm the Discovery twin uses to size the requirements/analysis phase. Applies to new estimates; other twins are unchanged."
      radioName="discovery-sizing-method"
      labels={DISCOVERY_SIZING_LABELS}
      load={getDiscoverySizingMethod}
      save={saveDiscoverySizingMethod}
    />
  );
}

function DevelopmentSizingSection() {
  return (
    <SizingMethodSection
      title="Development estimation method"
      description="The sizing algorithm the Development twin uses to convert scope into effort. Applies to new estimates; other twins are unchanged."
      radioName="dev-sizing-method"
      labels={DEV_SIZING_LABELS}
      load={getDevelopmentSizingMethod}
      save={saveDevelopmentSizingMethod}
    />
  );
}

function QaSizingSection() {
  return (
    <SizingMethodSection
      title="QA / testing estimation method"
      description="The sizing algorithm the QA & Testing twin uses to size test effort. Applies to new estimates; other twins are unchanged."
      radioName="qa-sizing-method"
      labels={QA_SIZING_LABELS}
      load={getQaSizingMethod}
      save={saveQaSizingMethod}
    />
  );
}

function StaffingCoefficientsSection() {
  const [rows, setRows] = useState<StaffingCoefficientRow[]>([]);
  const [editable, setEditable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    getStaffingCoefficients()
      .then((r) => {
        setRows(r.coefficients);
        setEditable(r.editable);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  const updateRow = (idx: number, value: number) => {
    setRows((rs) => rs.map((r, i) => (i === idx ? { ...r, value } : r)));
    setSaved(false);
  };

  const differs = rows.some((r) => r.value !== r.default_value);
  const hasInvalid = rows.some((r) => r.value < r.min_value || r.value > r.max_value);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const resp = await saveStaffingCoefficients(
        rows.map((r) => ({ key: r.key, value: r.value })),
      );
      setRows(resp.coefficients);
      setEditable(resp.editable);
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const resetToDefaults = () => {
    setRows((rs) => rs.map((r) => ({ ...r, value: r.default_value })));
    setSaved(false);
  };

  if (loading) return null;

  return (
    <section className="card space-y-3">
      <div>
        <h2 className="section-title">
          Team scaling (Brooks&apos;s Law + diminishing returns)
        </h2>
        <p className="text-xs muted">
          Coordination overhead inflates cost + schedule as the team grows; the
          diminishing-returns exponent (β&lt;1) shapes the duration curve and the
          recommended team size.
        </p>
      </div>

      {!editable && (
        <p className="text-xs text-amber-700">
          Postgres isn&apos;t connected — these are the in-code defaults, read-only.
        </p>
      )}
      {error && <p className="text-sm text-rose-600">{error}</p>}

      <table className="min-w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase muted">
            <th className="py-1">Coefficient</th>
            <th className="py-1 w-28">Value</th>
            <th className="py-1">Range · default</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const meta = COEFF_META[r.key];
            const invalid = r.value < r.min_value || r.value > r.max_value;
            return (
              <tr key={r.key} className="border-t border-slate-100 align-top">
                <td className="py-1.5">
                  <div className="font-medium">{meta?.label ?? r.key}</div>
                  {meta && <div className="text-[10px] muted">{meta.hint}</div>}
                  {r.is_override && (
                    <span className="text-[10px] uppercase tracking-wide text-brand-600">
                      edited
                    </span>
                  )}
                </td>
                <td className="py-1.5">
                  <input
                    type="number"
                    min={r.min_value}
                    max={r.max_value}
                    step={meta?.step ?? 0.01}
                    disabled={!editable}
                    value={r.value}
                    onChange={(e) => updateRow(i, Number(e.target.value))}
                    className={`input py-1 ${invalid ? "border-rose-400" : ""} disabled:opacity-60`}
                    aria-label={r.key}
                  />
                </td>
                <td className="py-1.5 text-xs muted">
                  {r.min_value}–{r.max_value} · {r.default_value}
                  {invalid && <span className="ml-2 text-rose-600">out of range</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs muted" role="status" aria-live="polite">
          {hasInvalid
            ? "Fix the out-of-range values before saving."
            : saved
              ? "Saved."
              : ""}
        </p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={resetToDefaults}
            disabled={!editable || !differs}
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
    </section>
  );
}

// Custom-role rows carry a client-only `_key` for stable React keys while a freshly-added row has
// no server role_id yet (the server assigns one on save).
type CustomRoleDraft = CustomRoleRow & { _key: string };

function DefaultRatesSection() {
  const [rows, setRows] = useState<RateRow[]>([]);
  const [customRoles, setCustomRoles] = useState<CustomRoleDraft[]>([]);
  const [editable, setEditable] = useState(true);
  const [bounds, setBounds] = useState({ min: 0, max: 1000 });
  const [loading, setLoading] = useState(true);
  // Did the initial GET succeed? Saving is blocked until it does, so a failed load (which leaves
  // rows/customRoles empty) can't be saved — which would otherwise wipe every custom role (an
  // explicit empty custom_roles list is a delete-all) and clear the grid overrides.
  const [loadOk, setLoadOk] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  // Monotonic counter for client-only keys of not-yet-saved rows. A plain ref (not crypto.randomUUID,
  // which is undefined in insecure contexts — plain-HTTP LAN deploys — and would throw on add).
  const newRowSeq = useRef(0);

  const adoptCustomRoles = (cr: CustomRoleRow[]) =>
    setCustomRoles(cr.map((r) => ({ ...r, _key: `srv_${r.role_id}` })));

  useEffect(() => {
    getDefaultRates()
      .then((r) => {
        setRows(r.rates);
        adoptCustomRoles(r.custom_roles);
        setEditable(r.editable);
        setBounds({ min: r.min_rate, max: r.max_rate });
        setLoadOk(true);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  const updateRate = (category: string, seniority: string, rate: number) => {
    setRows((rs) =>
      rs.map((r) => (r.category === category && r.seniority === seniority ? { ...r, rate } : r)),
    );
    setSaved(false);
  };

  const addCustomRole = () => {
    newRowSeq.current += 1;
    setCustomRoles((cr) => [
      ...cr,
      { _key: `new_${newRowSeq.current}`, role_id: "", label: "", category: "engineering", seniority: "senior", rate: 165 },
    ]);
    setSaved(false);
  };
  const updateCustomRole = (key: string, patch: Partial<CustomRoleRow>) => {
    setCustomRoles((cr) => cr.map((r) => (r._key === key ? { ...r, ...patch } : r)));
    setSaved(false);
  };
  const removeCustomRole = (key: string) => {
    setCustomRoles((cr) => cr.filter((r) => r._key !== key));
    setSaved(false);
  };

  const differs = rows.some((r) => r.rate !== r.default_rate);
  const gridInvalid = rows.some((r) => r.rate < bounds.min || r.rate > bounds.max);
  const customInvalid = customRoles.some(
    (r) => !r.label.trim() || r.rate < bounds.min || r.rate > bounds.max,
  );
  const hasInvalid = gridInvalid || customInvalid;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const resp = await saveDefaultRates(
        rows.map((r) => ({ category: r.category, seniority: r.seniority, rate: r.rate })),
        customRoles.map((r) => ({
          role_id: r.role_id || undefined,
          label: r.label.trim(),
          category: r.category,
          seniority: r.seniority,
          rate: r.rate,
        })),
      );
      setRows(resp.rates);
      adoptCustomRoles(resp.custom_roles);
      setEditable(resp.editable);
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const resetToDefaults = () => {
    setRows((rs) => rs.map((r) => ({ ...r, rate: r.default_rate })));
    setSaved(false);
  };

  if (loading) return null;

  // The backend returns all 28 cells in a stable order; derive the matrix axes from them.
  const categories = [...new Set(rows.map((r) => r.category))];
  const seniorities = [...new Set(rows.map((r) => r.seniority))];

  return (
    <section className="card space-y-3">
      <div>
        <h2 className="section-title">Default rate card (USD / hr)</h2>
        <p className="text-xs muted">
          Standard blended hourly rates per role category × seniority. These seed every new
          estimate&apos;s roster (you can still override per estimate); project cost = Σ(role hours
          × rate).
        </p>
      </div>

      {!editable && (
        <p className="text-xs text-amber-700">
          Postgres isn&apos;t connected — these are the in-code defaults, read-only.
        </p>
      )}
      {error && <p className="text-sm text-rose-600">{error}</p>}

      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase muted">
              <th className="py-1">Category</th>
              {seniorities.map((s) => (
                <th key={s} className="py-1 w-24 text-right">
                  {ROLE_SENIORITY_LABELS[s as keyof typeof ROLE_SENIORITY_LABELS] ?? s}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {categories.map((cat) => (
              <tr key={cat} className="border-t border-slate-100">
                <td className="py-1.5 font-medium">
                  {ROLE_CATEGORY_LABELS[cat as keyof typeof ROLE_CATEGORY_LABELS] ?? cat}
                </td>
                {seniorities.map((sen) => {
                  const r = rows.find((x) => x.category === cat && x.seniority === sen);
                  if (!r) return <td key={sen} />;
                  const invalid = r.rate < bounds.min || r.rate > bounds.max;
                  return (
                    <td key={sen} className="py-1.5 text-right">
                      <input
                        type="number"
                        min={bounds.min}
                        max={bounds.max}
                        step={5}
                        disabled={!editable}
                        value={r.rate}
                        onChange={(e) => updateRate(cat, sen, Number(e.target.value))}
                        className={`input py-1 w-20 text-right disabled:opacity-60 ${
                          invalid ? "border-rose-400" : r.is_override ? "border-brand-300" : ""
                        }`}
                        aria-label={`${cat} ${sen} hourly rate`}
                      />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-2 border-t border-slate-100 pt-3">
        <div className="flex items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-medium">Custom roles</h3>
            <p className="text-xs muted">
              Named roles on top of the grid (e.g. &ldquo;Principal Architect&rdquo;, &ldquo;Scrum
              Master&rdquo;). They appear in the Stage&nbsp;2 roster editor&apos;s &ldquo;Add from
              catalog&rdquo; picker, prefilling description, category, seniority &amp; rate.
            </p>
          </div>
          <button
            type="button"
            onClick={addCustomRole}
            disabled={!editable}
            className="btn-secondary text-xs disabled:opacity-50 disabled:cursor-not-allowed"
          >
            + Add custom role
          </button>
        </div>

        {customRoles.length === 0 ? (
          <p className="text-xs muted">No custom roles yet.</p>
        ) : (
          <div className="space-y-2">
            {customRoles.map((r) => {
              const labelEmpty = !r.label.trim();
              const rateBad = r.rate < bounds.min || r.rate > bounds.max;
              return (
                <div
                  key={r._key}
                  className="grid grid-cols-12 gap-2 items-center rounded-md border border-slate-200 bg-white p-2"
                >
                  <input
                    type="text"
                    value={r.label}
                    disabled={!editable}
                    maxLength={120}
                    placeholder="Role name (e.g. Principal Architect)"
                    onChange={(e) => updateCustomRole(r._key, { label: e.target.value })}
                    className={`input py-1 col-span-5 disabled:opacity-60 ${labelEmpty ? "border-rose-400" : ""}`}
                    aria-label="Custom role name"
                  />
                  <select
                    value={r.category}
                    disabled={!editable}
                    onChange={(e) => updateCustomRole(r._key, { category: e.target.value })}
                    className="select py-1 col-span-3 disabled:opacity-60"
                    aria-label="Custom role category"
                  >
                    {ROLE_CATEGORY_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                  <select
                    value={r.seniority}
                    disabled={!editable}
                    onChange={(e) => updateCustomRole(r._key, { seniority: e.target.value })}
                    className="select py-1 col-span-2 disabled:opacity-60"
                    aria-label="Custom role seniority"
                  >
                    {ROLE_SENIORITY_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                  <input
                    type="number"
                    min={bounds.min}
                    max={bounds.max}
                    step={5}
                    value={r.rate}
                    disabled={!editable}
                    onChange={(e) => updateCustomRole(r._key, { rate: Number(e.target.value) })}
                    className={`input py-1 col-span-1 text-right disabled:opacity-60 ${rateBad ? "border-rose-400" : ""}`}
                    aria-label="Custom role hourly rate"
                  />
                  <button
                    type="button"
                    onClick={() => removeCustomRole(r._key)}
                    disabled={!editable}
                    className="btn-secondary text-xs col-span-1 flex items-center justify-center disabled:opacity-50"
                    aria-label={`Remove ${r.label || "custom role"}`}
                    title="Remove this custom role"
                  >
                    ✕
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs muted" role="status" aria-live="polite">
          {customInvalid
            ? "Custom roles need a name and an in-range rate."
            : gridInvalid
              ? `Rates must be ${bounds.min}–${bounds.max}.`
              : !loadOk
                ? "Couldn’t load the rate card."
                : saved
                  ? "Saved."
                  : ""}
        </p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={resetToDefaults}
            disabled={!editable || !differs}
            title="Revert the grid rates to their shipped defaults (custom roles are not affected)"
            className="btn-secondary text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Reset grid rates
          </button>
          <button
            type="button"
            onClick={save}
            disabled={!editable || !loadOk || hasInvalid || saving}
            className="btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>
    </section>
  );
}

function ContingencySection() {
  const [pct, setPct] = useState(0);
  const [bounds, setBounds] = useState({ min: 0, max: 100 });
  const [defaultPct, setDefaultPct] = useState(0);
  const [editable, setEditable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    getContingency()
      .then((r) => {
        setPct(r.contingency_pct);
        setBounds({ min: r.min_pct, max: r.max_pct });
        setDefaultPct(r.default_pct);
        setEditable(r.editable);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  const invalid = pct < bounds.min || pct > bounds.max;
  const differs = pct !== defaultPct;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const resp = await saveContingency(pct);
      setPct(resp.contingency_pct);
      setEditable(resp.editable);
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (loading) return null;

  return (
    <section className="card space-y-3">
      <div>
        <h2 className="section-title">Contingency reserve</h2>
        <p className="text-xs muted">
          A management buffer added on top of every estimate&apos;s total{" "}
          <span className="font-medium">cost and timeline</span> (hours and headcount are
          unchanged). Distinct from the Monte Carlo confidence band, which models estimation
          uncertainty — this is a deliberate reserve. 0% = none.
        </p>
      </div>

      {!editable && (
        <p className="text-xs text-amber-700">
          Postgres isn&apos;t connected — this shows the in-code default ({defaultPct}%), read-only.
        </p>
      )}
      {error && <p className="text-sm text-rose-600">{error}</p>}

      <div className="flex items-center gap-2">
        <input
          type="number"
          min={bounds.min}
          max={bounds.max}
          step={1}
          disabled={!editable}
          value={pct}
          onChange={(e) => {
            setPct(Number(e.target.value));
            setSaved(false);
          }}
          className={`input py-1 w-24 text-right disabled:opacity-60 ${invalid ? "border-rose-400" : ""}`}
          aria-label="Contingency reserve percent"
        />
        <span className="text-sm muted">% reserve</span>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs muted" role="status" aria-live="polite">
          {invalid ? `Must be ${bounds.min}–${bounds.max}%.` : saved ? "Saved." : ""}
        </p>
        <button
          type="button"
          onClick={save}
          disabled={!editable || invalid || saving || !differs}
          className="btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? "Saving…" : "Save changes"}
        </button>
      </div>
    </section>
  );
}

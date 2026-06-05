"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";

import { FieldHint } from "@/components/FieldHint";
import { RoleRosterEditor } from "@/components/RoleRosterEditor";
import { StageProgress } from "@/components/StageProgress";
import {
  DEFAULT_ROSTER,
  INDUSTRY_OPTIONS,
  REGULATORY_OPTIONS,
  type CustomRoleInput,
  type Stage2Input,
} from "@/lib/schemas";
import { loadDraft, saveDraft } from "@/lib/wizard-store";

interface PrefillState {
  prefilled: boolean;
  summary: string;
  ambiguity: number;
}

const DEFAULTS: Stage2Input = {
  industry: "",
  project_type: "greenfield",
  screen_count_estimate: undefined,
  integration_count: 0,
  integration_list: [],
  engagement_model: "tm",
  target_timeline_weeks: undefined,
  regulatory_requirements: [],
  roster: { roles: DEFAULT_ROSTER },
};

/** Detect whether the LLM actually contributed values, vs. returning a roster
 *  of defaults from the fallback path. Used to flip the banner from "prefilled"
 *  to "couldn't extract specifics" without changing the wire contract. */
function prefillIsEffective(s: Stage2Input): boolean {
  return (
    (s.industry?.trim().length ?? 0) > 0 ||
    (s.screen_count_estimate ?? 0) > 0 ||
    (s.integration_count ?? 0) > 0 ||
    (s.integration_list?.length ?? 0) > 0 ||
    (s.regulatory_requirements?.length ?? 0) > 0 ||
    s.project_type !== "greenfield"
  );
}

export default function Stage2DraftPage() {
  const router = useRouter();
  const { register, handleSubmit, setValue, watch, reset } = useForm<Stage2Input>({
    defaultValues: DEFAULTS,
  });
  const [prefill, setPrefill] = useState<PrefillState | null>(null);
  const [prefillEffective, setPrefillEffective] = useState(true);
  const [dismissedPrefill, setDismissedPrefill] = useState(false);

  useEffect(() => {
    const draft = loadDraft();
    if (draft?.stage2) {
      // `reset` (not per-key setValue) so uncontrolled inputs registered via
      // {...register(name)} actually repaint. setValue intentionally skips
      // re-renders and won't reliably propagate to <select> / <input> DOM nodes.
      const merged: Stage2Input = {
        ...DEFAULTS,
        ...draft.stage2,
        // JSON null → undefined so the optional number inputs render blank
        // rather than as a literal "null".
        screen_count_estimate:
          draft.stage2.screen_count_estimate ?? undefined,
        target_timeline_weeks:
          draft.stage2.target_timeline_weeks ?? undefined,
      };
      reset(merged);
      setPrefillEffective(prefillIsEffective(merged));
    }
    if (draft?.stage2_prefilled) {
      setPrefill({
        prefilled: true,
        summary: draft.prefill_summary ?? "",
        ambiguity: draft.prefill_ambiguity ?? 0.5,
      });
    }
  }, [reset]);

  const dismissPrefillBanner = () => {
    setDismissedPrefill(true);
    // Clear the prefill markers from the draft so revisiting Stage 2 doesn't
    // re-show the banner once the user has acknowledged + edited the values.
    const draft = loadDraft();
    if (draft) {
      saveDraft({
        ...draft,
        stage2_prefilled: false,
        prefill_summary: undefined,
        prefill_ambiguity: undefined,
      });
    }
  };

  const regs = watch("regulatory_requirements") || [];
  const roster = watch("roster")?.roles ?? DEFAULT_ROSTER;

  const setRoster = (next: CustomRoleInput[]) => {
    setValue("roster", { roles: next });
  };

  const rosterTotal = roster.reduce((a, r) => a + r.percentage, 0);
  const rosterValid =
    roster.length > 0 &&
    Math.abs(rosterTotal - 100) <= 0.5 &&
    new Set(roster.map((r) => r.role_id)).size === roster.length;

  const onSubmit = (values: Stage2Input) => {
    if (!rosterValid) return; // form button is disabled but defensive
    const draft = loadDraft();
    if (!draft) return router.push("/estimate/new");
    saveDraft({ ...draft, stage2: values });
    router.push("/estimate/draft/maturity");
  };

  const toggleReg = (r: string) => {
    setValue(
      "regulatory_requirements",
      regs.includes(r) ? regs.filter((x) => x !== r) : [...regs, r]
    );
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <StageProgress current={2} />
      <header className="space-y-2">
        <h1 className="text-2xl font-bold text-slate-900">Project context</h1>
        <p className="muted">
          Fill in what you know. Empty fields will be inferred during Pass 1. You
          can refine further on Stage 4.
        </p>
      </header>

      {prefill?.prefilled && !dismissedPrefill && (
        <div
          className={`rounded-md border p-3 text-sm space-y-2 ${
            !prefillEffective
              ? "border-slate-300 bg-slate-50 text-slate-800"
              : prefill.ambiguity >= 0.6
              ? "border-amber-300 bg-amber-50 text-amber-900"
              : "border-emerald-300 bg-emerald-50 text-emerald-900"
          }`}
          role="status"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-1">
              <p className="font-semibold">
                {!prefillEffective
                  ? "Couldn't extract specifics from your description."
                  : prefill.ambiguity >= 0.6
                  ? "Prefilled — confidence is low, double-check the values."
                  : "Prefilled from your description — review and edit anything that looks off."}
              </p>
              {!prefillEffective && (
                <p className="text-xs leading-snug">
                  Most likely the backend&apos;s <code>ANTHROPIC_API_KEY</code> isn&apos;t
                  set (the prefill endpoint silently fell back to defaults), or
                  the description was too brief / abstract to extract industry,
                  integrations, or regulatory mentions. Fill the fields in
                  manually and continue.
                </p>
              )}
              {prefillEffective && prefill.summary && (
                <p className="text-xs italic leading-snug">
                  Interpreted as: “{prefill.summary}”
                </p>
              )}
              <p className="text-[10px] muted">
                Ambiguity score: {Math.round(prefill.ambiguity * 100)}%
              </p>
            </div>
            <button
              type="button"
              onClick={dismissPrefillBanner}
              className="text-xs underline hover:no-underline shrink-0"
              aria-label="Dismiss prefill banner"
            >
              Got it
            </button>
          </div>
        </div>
      )}

      <form onSubmit={handleSubmit(onSubmit)} className="card space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="label inline-flex items-center">
              Industry
              <FieldHint text="The primary business domain. Twins use this to anchor LLM extraction (e.g. mention of HIPAA in healthcare) and to filter calibration aggregates against prior similar projects." />
            </label>
            <select className="select mt-1" {...register("industry")}>
              <option value="">— pick one —</option>
              {INDUSTRY_OPTIONS.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label inline-flex items-center">
              Project type
              <FieldHint text="Greenfield: new build. Legacy replacement: rewriting an existing system. Enhancement: features on a live product. Integration: wiring third-party systems. Data migration: moving data between stores. AI/ML build: model + supporting infrastructure." />
            </label>
            <select className="select mt-1" {...register("project_type")}>
              <option value="greenfield">Greenfield</option>
              <option value="legacy_replacement">Legacy replacement</option>
              <option value="enhancement">Enhancement</option>
              <option value="integration">Integration</option>
              <option value="data_migration">Data migration</option>
              <option value="ai_ml_build">AI / ML build</option>
            </select>
          </div>
          <div>
            <label className="label inline-flex items-center">
              Estimated screens
              <FieldHint text="Rough count of distinct UI screens or pages. Used by the UX twin's SCP algorithm and contributes to the Discovery twin's UCP use-case count." />
            </label>
            <input
              type="number"
              min={0}
              className="input mt-1"
              {...register("screen_count_estimate", { valueAsNumber: true })}
            />
          </div>
          <div>
            <label className="label inline-flex items-center">
              Integration count
              <FieldHint text="Number of external systems the project will integrate with (APIs, databases, third-party SDKs, identity providers). Drives Deployment, Code Review, and QA scoping." />
            </label>
            <input
              type="number"
              min={0}
              className="input mt-1"
              {...register("integration_count", { valueAsNumber: true })}
            />
          </div>
          <div>
            <label className="label inline-flex items-center">
              Engagement model
              <FieldHint text="T&M: pay for hours worked. Fixed price: lump sum for an agreed scope. Retainer: ongoing monthly capacity. Hybrid: T&M for discovery then fixed for build. Used by Stage 5 to format the commercial summary." />
            </label>
            <select className="select mt-1" {...register("engagement_model")}>
              <option value="tm">Time & materials</option>
              <option value="fixed_price">Fixed price</option>
              <option value="retainer">Retainer</option>
              <option value="hybrid">Hybrid</option>
            </select>
          </div>
          <div>
            <label className="label inline-flex items-center">
              Target timeline (weeks)
              <FieldHint text="Desired project duration in weeks. Synthesize uses this to derive recommended headcount per role and the weekly burn rate. Leave empty to default to a 5-person team capacity calc." />
            </label>
            <input
              type="number"
              min={1}
              className="input mt-1"
              {...register("target_timeline_weeks", { valueAsNumber: true })}
            />
          </div>
        </div>

        <div>
          <label className="label inline-flex items-center">
            Regulatory requirements
            <FieldHint text="Compliance regimes that apply. Triggers extra hours in Deployment (audit-ready infra), QA (compliance testing), and Discovery (regulatory analysis). Also influences the QA twin's plan recommendation." />
          </label>
          <div className="mt-2 flex flex-wrap gap-2">
            {REGULATORY_OPTIONS.map((r) => (
              <button
                type="button"
                key={r}
                onClick={() => toggleReg(r)}
                className={`px-3 py-1 rounded-full text-xs border transition ${
                  regs.includes(r)
                    ? "bg-brand-600 text-white border-brand-600"
                    : "bg-white text-slate-700 border-slate-300 hover:bg-slate-50"
                }`}
              >
                {r}
              </button>
            ))}
          </div>
        </div>

        <RoleRosterEditor value={roster} onChange={setRoster} />

        <div className="flex items-center justify-between pt-2">
          <button
            type="button"
            onClick={() => router.push("/estimate/new")}
            className="btn-secondary"
          >
            Back
          </button>
          <button
            type="submit"
            disabled={!rosterValid}
            className="btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
            title={
              !rosterValid
                ? "Roster must have at least one role and percentages summing to 100"
                : undefined
            }
          >
            Continue to maturity
          </button>
        </div>
      </form>
    </div>
  );
}

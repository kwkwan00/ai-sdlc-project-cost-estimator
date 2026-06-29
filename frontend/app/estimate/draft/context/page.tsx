"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";

import { FieldHint } from "@/components/FieldHint";
import { RoleRosterEditor } from "@/components/RoleRosterEditor";
import { RosterRationaleModal } from "@/components/RosterRationaleModal";
import { StageProgress } from "@/components/StageProgress";
import { proposeRoster, type RosterPlanItem } from "@/lib/roster-agui";
import {
  INDUSTRY_OPTIONS,
  REGULATORY_OPTIONS,
  type CustomRoleInput,
  type Stage2Input,
} from "@/lib/schemas";
import { currentWizardSession, loadDraft, saveDraft } from "@/lib/wizard-store";

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
  // Start with NO roster entries. The team is proposed asynchronously by the
  // AG-UI roster agent on Stage 2; nothing is shown until that run completes
  // (or the user adds roles / proposes manually). Avoids flashing placeholder
  // defaults that get immediately replaced.
  roster: { roles: [] },
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
  // AG-UI roster agent (Option B): proposes a tailored team after prefill.
  const [rosterLoading, setRosterLoading] = useState(false);
  const [rosterModal, setRosterModal] = useState<{
    rationale: string;
    projectPlan: RosterPlanItem[];
  } | null>(null);
  const [rosterError, setRosterError] = useState<string | null>(null);
  const rosterStartedRef = useRef(false); // auto-run start-once guard
  const mountedRef = useRef(true); // re-set true on strict-mode remount

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

  // Run the AG-UI roster agent and apply its proposed team. Used both by the
  // auto-run effect (after prefill) and the manual "Propose team" / retry button.
  const runRosterProposal = useCallback(async () => {
    const draft = loadDraft();
    if (!draft?.stage2 || !draft.raw_input) return;
    const stage2 = draft.stage2;
    const rawInput = draft.raw_input;
    // Scope the proposal to the phases chosen on Stage 1 (the backend ignores a full/empty set).
    const selectedPhases = draft.selected_phases;
    setRosterError(null);
    setRosterLoading(true);
    try {
      const result = await proposeRoster({
        stage2,
        rawInput,
        selectedPhases,
        sessionId: currentWizardSession(),
      });
      if (!mountedRef.current) return;
      setValue("roster", { roles: result.roster });
      const latest = loadDraft();
      if (latest) {
        saveDraft({
          ...latest,
          stage2: { ...(latest.stage2 ?? stage2), roster: { roles: result.roster } },
          // Persist the success marker ONLY here so a failed run can retry on the
          // next visit instead of poisoning the draft into showing defaults forever.
          roster_proposed: true,
        });
      }
      if (result.rationale || result.projectPlan.length > 0) {
        setRosterModal({
          rationale: result.rationale,
          projectPlan: result.projectPlan,
        });
      }
    } catch (e) {
      // Failed (no API key, RUN_ERROR, network, unreachable backend) — keep the
      // default roster, surface the reason, and leave roster_proposed UNSET so
      // the auto-run retries next visit and the button can retry now.
      if (mountedRef.current) {
        setRosterError((e as Error)?.message || "Couldn't propose a team.");
      }
    } finally {
      if (mountedRef.current) setRosterLoading(false);
    }
  }, [setValue]);

  // Auto-fire the proposal once, after prefill, to propose a tailored team. The
  // roster editor stays locked (disabled) until the proposal lands or fails.
  useEffect(() => {
    mountedRef.current = true;
    if (!rosterStartedRef.current) {
      const draft = loadDraft();
      const shouldRun = Boolean(
        draft?.stage2_prefilled &&
          !draft.roster_proposed &&
          draft.stage2 &&
          draft.raw_input
      );
      if (shouldRun) {
        rosterStartedRef.current = true;
        console.debug("[roster-agui] auto-running proposal on Stage 2 mount");
        void runRosterProposal();
      } else if (draft?.stage2_prefilled) {
        // Came from prefill but auto-run was gated out — log why (helps explain
        // a roster that "doesn't populate", e.g. already proposed on a revisit).
        console.debug("[roster-agui] auto-run skipped", {
          roster_proposed: draft.roster_proposed,
          has_stage2: Boolean(draft.stage2),
          has_raw_input: Boolean(draft.raw_input),
        });
      }
    }
    return () => {
      mountedRef.current = false;
    };
  }, [runRosterProposal]);

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
  const roster = watch("roster")?.roles ?? [];

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

        <div className="space-y-2">
          {rosterLoading ? (
            <div
              className="flex items-center gap-2 rounded-md border border-brand-200 bg-brand-50 p-2 text-xs text-brand-800"
              role="status"
              aria-live="polite"
            >
              <svg
                className="h-4 w-4 animate-spin shrink-0"
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
                />
              </svg>
              Proposing a tailored team from your project description…
            </div>
          ) : (
            <>
              <button
                type="button"
                onClick={() => void runRosterProposal()}
                className="btn-secondary text-xs inline-flex items-center gap-1"
              >
                ✨ {rosterError ? "Retry proposing team" : "Propose team from description"}
              </button>
              {rosterError && (
                <p className="text-xs text-amber-700" role="status" aria-live="polite">
                  Couldn&apos;t propose a tailored team automatically ({rosterError}).
                  Retry above, or add roles manually below.
                </p>
              )}
              {/* Editor renders only once the run completes — no entries are shown
                  while the AG-UI proposal is in flight. */}
              <RoleRosterEditor value={roster} onChange={setRoster} />
            </>
          )}
        </div>

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
            disabled={!rosterValid || rosterLoading}
            className="btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
            title={
              rosterLoading
                ? "Proposing a tailored team — one moment…"
                : !rosterValid
                ? "Roster must have at least one role and percentages summing to 100"
                : undefined
            }
          >
            Continue to maturity
          </button>
        </div>
      </form>

      <RosterRationaleModal
        open={!!rosterModal}
        rationale={rosterModal?.rationale ?? ""}
        projectPlan={rosterModal?.projectPlan ?? []}
        onClose={() => setRosterModal(null)}
      />
    </div>
  );
}

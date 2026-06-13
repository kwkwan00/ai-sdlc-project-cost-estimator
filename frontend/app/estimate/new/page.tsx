"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useForm } from "react-hook-form";

import { prefillFromDescription } from "@/lib/api-client";
import { stage1Schema, type Stage1Input } from "@/lib/schemas";
import { loadDraft, saveDraft } from "@/lib/wizard-store";

const HEALTHCARE_EXAMPLE = `We need to build a HIPAA-compliant patient portal for a regional clinic. Patients should be able to view their lab results, schedule appointments, message their provider, request prescription refills, and view billing. Clinic staff need an admin view to manage appointment availability and review messages.

Estimated 25 screens covering 4 user roles (patient, provider, billing admin, scheduler). Integrations: Epic EHR (FHIR), Stripe billing, Twilio SMS for reminders. The clinic already uses Okta for SSO. They want responsive web (no mobile app initially).`;

function Stage1Inner() {
  const router = useRouter();
  const params = useSearchParams();
  const quick = params.get("quick") === "1";

  const { register, handleSubmit, setValue, formState } = useForm<Stage1Input>({
    resolver: zodResolver(stage1Schema),
    defaultValues: { raw_input: "", project_name: "" },
  });

  const [analyzing, setAnalyzing] = useState(false);
  const [prefillNote, setPrefillNote] = useState<string | null>(null);

  useEffect(() => {
    const draft = loadDraft();
    if (draft) {
      setValue("raw_input", draft.raw_input || "");
      setValue("project_name", draft.project_name || "");
    }
  }, [setValue]);

  const onSubmit = async (values: Stage1Input) => {
    if (quick) {
      // Quick mode bypasses Stage 2/3 entirely — skip the prefill call so we
      // don't burn an LLM round-trip the user will immediately discard.
      saveDraft({
        raw_input: values.raw_input,
        project_name: values.project_name,
      });
      router.push(`/estimate/draft/create?quick=1`);
      return;
    }

    setAnalyzing(true);
    setPrefillNote(null);
    try {
      const prefill = await prefillFromDescription(values.raw_input);
      saveDraft({
        raw_input: values.raw_input,
        project_name: values.project_name,
        // Prefill is roster-free, and we no longer seed placeholder roles — the
        // roster starts empty and is populated by the AG-UI proposal on Stage 2.
        stage2: { ...prefill.stage2, roster: { roles: [] } },
        stage2_prefilled: true,
        prefill_ambiguity: prefill.ambiguity_score,
        prefill_summary: prefill.summary,
        // Carry any AI tools named in the description forward to seed Stage 3.
        prefill_ai_tooling: prefill.ai_tooling_description,
      });
      router.push(`/estimate/draft/context`);
    } catch (e) {
      // Graceful degradation: if the LLM call fails (network, backend down)
      // we save the raw input and continue. Stage 2 will render its defaults.
      saveDraft({
        raw_input: values.raw_input,
        project_name: values.project_name,
      });
      setPrefillNote(
        `Couldn't auto-fill from description (${(e as Error).message}). Continuing with a blank form.`
      );
      setAnalyzing(false);
      // Brief pause so the user sees the message before the route change.
      setTimeout(() => router.push(`/estimate/draft/context`), 1200);
    }
  };

  const useExample = () => {
    setValue("raw_input", HEALTHCARE_EXAMPLE);
    setValue("project_name", "Healthcare patient portal");
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <header className="space-y-2">
        <p className="text-xs uppercase tracking-wide muted">Stage 1 of 5</p>
        <h1 className="text-2xl font-bold text-slate-900">
          Describe the project
        </h1>
        <p className="muted">
          Paste sales notes, RFP excerpts, meeting summaries — anything that
          captures the scope. The AI parser will extract structured signals; you
          can review and refine them on the next page.
        </p>
      </header>

      <form onSubmit={handleSubmit(onSubmit)} className="card space-y-5">
        <div>
          <label className="label" htmlFor="project_name">
            Project name <span className="muted">(optional)</span>
          </label>
          <input
            id="project_name"
            type="text"
            className="input mt-1"
            placeholder="e.g. Healthcare patient portal"
            {...register("project_name")}
          />
        </div>

        <div>
          <div className="flex items-center justify-between">
            <label className="label" htmlFor="raw_input">
              Project description
            </label>
            <button
              type="button"
              onClick={useExample}
              className="text-xs text-brand-600 hover:underline"
            >
              Use healthcare example
            </button>
          </div>
          <textarea
            id="raw_input"
            className="textarea mt-1"
            placeholder="Describe the project in plain English..."
            {...register("raw_input")}
          />
          {formState.errors.raw_input && (
            <p className="help text-rose-600">
              {formState.errors.raw_input.message}
            </p>
          )}
          <p className="help">
            Tip: include user roles, screen estimates, integrations, and
            regulatory requirements if known.
          </p>
        </div>

        {prefillNote && (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800">
            {prefillNote}
          </div>
        )}

        <div className="flex items-center justify-between pt-2">
          <p className="text-xs muted">
            {analyzing
              ? "Analyzing description with Claude…"
              : quick
              ? "Quick mode — Stages 2 + 3 will be skipped with defaults."
              : "Next: project context (Stage 2). The description is auto-analyzed to prefill the form."}
          </p>
          <button
            className="btn-primary disabled:opacity-60 disabled:cursor-progress"
            type="submit"
            disabled={analyzing}
          >
            {analyzing ? "Analyzing…" : quick ? "Generate estimate" : "Continue"}
          </button>
        </div>
      </form>
    </div>
  );
}

export default function Stage1Page() {
  return (
    <Suspense fallback={<div className="card max-w-xl">Loading...</div>}>
      <Stage1Inner />
    </Suspense>
  );
}

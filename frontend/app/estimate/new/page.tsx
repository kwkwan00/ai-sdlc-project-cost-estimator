"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useForm } from "react-hook-form";

import { DocumentUpload } from "@/components/DocumentUpload";
import { prefillFromDescription } from "@/lib/api-client";
import {
  EXAMPLES,
  SIZE_ORDER,
  type ExampleProject,
} from "@/lib/example-projects";
import { ALL_PHASES, PhaseScopePicker } from "@/components/PhaseScopePicker";
import { stage1Schema, type Stage1Input } from "@/lib/schemas";
import { type Phase } from "@/lib/types";
import { loadDraft, saveDraft, startWizardSession } from "@/lib/wizard-store";

function Stage1Inner() {
  const router = useRouter();

  const { register, handleSubmit, setValue, getValues, formState } = useForm<Stage1Input>({
    resolver: zodResolver(stage1Schema),
    defaultValues: { raw_input: "", project_name: "" },
  });

  const [analyzing, setAnalyzing] = useState(false);
  const [prefillNote, setPrefillNote] = useState<string | null>(null);
  const [pick, setPick] = useState("");
  // Which SDLC phases to estimate. All selected by default; the request omits the field entirely
  // when all six remain checked, so a full-scope estimate is byte-identical to the pre-feature one.
  const [selectedPhases, setSelectedPhases] = useState<Phase[]>(ALL_PHASES);

  useEffect(() => {
    const draft = loadDraft();
    if (draft) {
      setValue("raw_input", draft.raw_input || "");
      setValue("project_name", draft.project_name || "");
      // Restore a previously-chosen phase subset so returning here doesn't silently reset the
      // scope to all six. Absent/empty ⇒ keep the all-selected default.
      if (draft.selected_phases?.length) setSelectedPhases(draft.selected_phases);
    }
  }, [setValue]);

  const onSubmit = async (values: Stage1Input) => {
    setAnalyzing(true);
    setPrefillNote(null);
    // Start a fresh wizard-run UUID up front so every pre-submission LLM call
    // (this prefill, the Stage 2 roster, the Stage 3 tooling classifier) and the
    // final create all share one id — the backend associates them in Observability.
    const sessionId = startWizardSession();
    try {
      const prefill = await prefillFromDescription(values.raw_input, sessionId);
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
        selected_phases: selectedPhases,
      });
      router.push(`/estimate/draft/context`);
    } catch (e) {
      // Graceful degradation: if the LLM call fails (network, backend down)
      // we save the raw input and continue. Stage 2 will render its defaults.
      saveDraft({
        raw_input: values.raw_input,
        project_name: values.project_name,
        selected_phases: selectedPhases,
      });
      setPrefillNote(
        `Couldn't auto-fill from description (${(e as Error).message}). Continuing with a blank form.`
      );
      setAnalyzing(false);
      // Brief pause so the user sees the message before the route change.
      setTimeout(() => router.push(`/estimate/draft/context`), 1200);
    }
  };

  const applyExample = (ex: ExampleProject) => {
    setValue("raw_input", ex.description);
    setValue("project_name", ex.name);
  };

  // Uploaded document → fill the (editable) description; derive a project name from the file
  // name if one isn't set yet.
  const onDocumentText = (text: string, fileName: string) => {
    setValue("raw_input", text, { shouldValidate: true, shouldDirty: true });
    if (!getValues("project_name")) {
      const base = fileName.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim();
      if (base) setValue("project_name", base.slice(0, 80));
    }
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
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label className="label" htmlFor="raw_input">
              Project description
            </label>
            <select
              aria-label="Prefill an example project"
              value={pick}
              onChange={(e) => {
                const ex = EXAMPLES.find((x) => x.name === e.target.value);
                if (ex) applyExample(ex);
                setPick(""); // action menu — reset so any example can be re-picked
              }}
              className="input max-w-[15rem] py-1 text-sm"
            >
              <option value="">Prefill an example…</option>
              {SIZE_ORDER.map((size) => (
                <optgroup key={size} label={size}>
                  {EXAMPLES.filter((ex) => ex.size === size).map((ex) => (
                    <option key={ex.name} value={ex.name}>
                      {ex.label}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </div>
          <div className="mt-1">
            <DocumentUpload onExtracted={onDocumentText} />
          </div>
          <textarea
            id="raw_input"
            className="textarea mt-2"
            placeholder="Describe the project in plain English — or upload a document above."
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

        <div className="border-t border-slate-100 pt-4">
          <PhaseScopePicker selected={selectedPhases} onChange={setSelectedPhases} />
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
              : "Next: project context (Stage 2). The description is auto-analyzed to prefill the form."}
          </p>
          <button
            className="btn-primary disabled:opacity-60 disabled:cursor-progress"
            type="submit"
            disabled={analyzing || selectedPhases.length === 0}
          >
            {analyzing ? "Analyzing…" : "Continue"}
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

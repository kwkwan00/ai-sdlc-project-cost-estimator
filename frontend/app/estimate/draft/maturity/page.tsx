"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { StageProgress } from "@/components/StageProgress";
import {
  buildCreatePayload,
  classifyTooling,
  createEstimate,
} from "@/lib/api-client";
import {
  CODEBASE_CONTEXT_LABELS,
  type CodebaseContext,
  type PhaseTooling,
  type Stage3Input,
} from "@/lib/schemas";
import { PHASE_LABELS, type Phase } from "@/lib/types";
import { clearDraft, loadDraft, saveDraft, saveSession } from "@/lib/wizard-store";

// Canonical phase order for the scope picker (mirrors the review page's label source).
const ALL_PHASES = Object.keys(PHASE_LABELS) as Phase[];

const NO_TOOLING: PhaseTooling = {
  discovery: "none",
  ux_design: "none",
  development: "none",
  code_review: "none",
  deployment: "none",
  qa_testing: "none",
};

const DEFAULT: Stage3Input = {
  codebase_context: "greenfield",
  ai_tooling_description: "",
  ai_tooling: { ...NO_TOOLING },
  technology_stack: "",
};

export default function Stage3DraftPage() {
  const router = useRouter();
  const [stage3, setStage3] = useState<Stage3Input>(DEFAULT);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const draft = loadDraft();
    if (!draft) return;
    const base: Stage3Input = draft.stage3
      ? { ...DEFAULT, ...draft.stage3 }
      : { ...DEFAULT };
    // Seed the tooling textarea from any AI tools the prefill found in the Stage 1
    // description — but only if the user hasn't already typed/edited their own.
    if (!base.ai_tooling_description && draft.prefill_ai_tooling) {
      base.ai_tooling_description = draft.prefill_ai_tooling;
    }
    setStage3(base);
  }, []);

  const persistAndCreate = async () => {
    const draft = loadDraft();
    if (!draft) {
      router.push("/estimate/new");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      // Classify the freeform tooling description into per-phase levels. The
      // backend always returns a valid mapping; only a network failure throws —
      // fall back to no AI tooling rather than blocking the estimate.
      let ai_tooling = { ...NO_TOOLING };
      const description = stage3.ai_tooling_description.trim();
      if (description) {
        try {
          ai_tooling = (await classifyTooling(description)).ai_tooling;
        } catch {
          ai_tooling = { ...NO_TOOLING };
        }
      }
      const classifiedStage3: Stage3Input = { ...stage3, ai_tooling };

      // Phase scope is chosen on Stage 1 and rides in the draft. Treat an absent OR empty list as
      // full scope (a stored `[]` must not pass through as an empty request, which the backend 422s)
      // and omit the field when every phase is chosen so the full-scope request stays byte-identical.
      const selectedPhases = draft.selected_phases?.length ? draft.selected_phases : ALL_PHASES;
      const phasesArg =
        selectedPhases.length === ALL_PHASES.length ? undefined : selectedPhases;
      const payload = buildCreatePayload(
        draft.raw_input,
        draft.project_name,
        draft.stage2,
        classifiedStage3,
        phasesArg
      );
      const envelope = await createEstimate(payload);
      saveSession(envelope.estimate_id, {
        raw_input: draft.raw_input,
        project_name: draft.project_name,
        stage2: draft.stage2,
        stage3: classifiedStage3,
      });
      clearDraft();
      router.push(`/estimate/${envelope.estimate_id}/questions`);
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <StageProgress current={3} />
      <header className="space-y-2">
        <h1 className="text-2xl font-bold text-slate-900">
          AI acceleration context
        </h1>
        <p className="muted">
          Tell us about the codebase and the AI tooling the team will use. These
          two settings drive the AI-acceleration estimate. Team composition lives
          in Stage 2 (Project context) — adjust the roster there.
        </p>
      </header>

      <div className="card space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="label inline-flex items-center">
              Codebase context
            </label>
            <select
              className="select mt-1"
              value={stage3.codebase_context}
              onChange={(e) =>
                setStage3({
                  ...stage3,
                  codebase_context: e.target.value as CodebaseContext,
                })
              }
            >
              {(
                Object.entries(CODEBASE_CONTEXT_LABELS) as [
                  CodebaseContext,
                  string,
                ][]
              ).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <p className="help">
              Drives the AI-acceleration estimate: how much of the codebase the
              team already understands.
            </p>
          </div>

          <div>
            <label className="label" htmlFor="technology-stack">
              Existing / proposed technologies
            </label>
            <textarea
              id="technology-stack"
              className="textarea mt-1 min-h-[4.5rem]"
              placeholder="e.g. React + Node, Java/Spring, Postgres, AWS, a Kafka pipeline. Leave blank if undecided."
              value={stage3.technology_stack}
              onChange={(e) =>
                setStage3({ ...stage3, technology_stack: e.target.value })
              }
            />
            <p className="help">
              Languages, frameworks, cloud, and datastores the client already uses or
              plans to use. Helps size the effort and lets the estimate reference the
              real stack.
            </p>
          </div>
        </div>

        <div className="space-y-3">
          <div className="space-y-1">
            <h2 className="text-lg font-semibold text-slate-900">
              AI tooling
            </h2>
            <p className="muted">
              Describe the AI tools your team uses and where. We&apos;ll map them
              to each SDLC phase automatically — tooling differs by stage, so a
              team may have agentic coding for development but nothing for UX.
            </p>
          </div>
          <div>
            <label className="label" htmlFor="ai-tooling">
              AI tools you use
            </label>
            <textarea
              id="ai-tooling"
              className="textarea mt-1 min-h-[7rem]"
              placeholder="e.g. Claude Code for development and PR reviews, Figma AI for design, CodeRabbit on pull requests, Harness.io for deploys, LangSmith for test eval. Leave blank if the team uses no AI tooling."
              value={stage3.ai_tooling_description}
              onChange={(e) =>
                setStage3({
                  ...stage3,
                  ai_tooling_description: e.target.value,
                })
              }
            />
            <p className="help">
              Free text — name the tools and what you use them for. We classify
              each into a per-phase AI level on submit; unrecognized tools are
              looked up automatically.
            </p>
          </div>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-rose-300 bg-rose-50 p-3 text-sm text-rose-700">
          {error}
        </div>
      )}

      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => {
            // Persist the Stage-3 edits so returning to this page restores them instead of
            // silently resetting. Phase scope is owned by Stage 1 and left untouched here.
            const draft = loadDraft();
            if (draft) {
              saveDraft({ ...draft, stage3 });
            }
            router.push("/estimate/draft/context");
          }}
          className="btn-secondary"
        >
          Back
        </button>
        <button
          type="button"
          onClick={persistAndCreate}
          disabled={submitting}
          className="btn-primary"
        >
          {submitting ? "Starting Pass 1..." : "Generate estimate"}
        </button>
      </div>
    </div>
  );
}

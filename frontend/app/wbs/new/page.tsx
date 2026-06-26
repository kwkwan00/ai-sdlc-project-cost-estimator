"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  EXAMPLES,
  SIZE_ORDER,
  type ExampleProject,
} from "@/lib/example-projects";
import {
  CODEBASE_CONTEXT_LABELS,
  type CodebaseContext,
} from "@/lib/schemas";
import { ALL_PHASES, PhaseScopePicker } from "@/components/PhaseScopePicker";
import { type Phase } from "@/lib/types";
import { saveWbsNewDraft } from "@/lib/wbs-store";

const CODEBASE_OPTIONS = Object.keys(CODEBASE_CONTEXT_LABELS) as CodebaseContext[];

export default function NewWbsPage() {
  const router = useRouter();
  const [projectName, setProjectName] = useState("");
  const [rawInput, setRawInput] = useState("");
  const [codebase, setCodebase] = useState<CodebaseContext>("greenfield");
  const [technology, setTechnology] = useState("");
  const [tooling, setTooling] = useState("");
  const [pick, setPick] = useState("");
  // SDLC phases to draft. All selected by default; a strict subset scopes the LLM-drafted tree so
  // disabled phases produce no work packages.
  const [selectedPhases, setSelectedPhases] = useState<Phase[]>(ALL_PHASES);

  const canSubmit = rawInput.trim().length >= 10 && selectedPhases.length > 0;

  function applyExample(ex: ExampleProject) {
    setRawInput(ex.description);
    if (!projectName) setProjectName(ex.name);
  }

  function handleContinue() {
    // Persist the full array when all phases are chosen so the team page can omit it for a
    // full-scope draft (the backend treats a full/empty set as "no constraint").
    saveWbsNewDraft({
      project_name: projectName,
      raw_input: rawInput,
      tooling,
      codebase,
      technology,
      selected_phases: selectedPhases,
    });
    router.push("/wbs/team");
  }

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <p className="text-xs uppercase tracking-wide muted">Step 1 of 3</p>
        <h1 className="text-2xl font-bold text-slate-900">New WBS estimate</h1>
        <p className="text-slate-600 max-w-2xl">
          Describe the project and your AI tooling. Next, we&apos;ll propose a team from your
          description, then an AI assistant drafts a Work Breakdown Structure you can edit.
        </p>
      </div>

      <section className="card space-y-4">
        <label className="block">
          <span className="label">Project name</span>
          <input
            className="input"
            value={projectName}
            onChange={(e) => setProjectName(e.target.value)}
            placeholder="e.g. Patient intake portal"
          />
        </label>

        <div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="label">Project description</span>
            <select
              aria-label="Prefill an example project"
              value={pick}
              onChange={(e) => {
                const ex = EXAMPLES.find((x) => x.name === e.target.value);
                if (ex) applyExample(ex);
                setPick("");
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
          <textarea
            className="input min-h-[8rem] mt-1"
            value={rawInput}
            onChange={(e) => setRawInput(e.target.value)}
            placeholder="Describe the scope, key features, integrations, and constraints… or prefill an example."
          />
        </div>

        <label className="block">
          <span className="label">Codebase context</span>
          <select
            className="input"
            value={codebase}
            onChange={(e) => setCodebase(e.target.value as CodebaseContext)}
          >
            {CODEBASE_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {CODEBASE_CONTEXT_LABELS[c]}
              </option>
            ))}
          </select>
        </label>

        <PhaseScopePicker
          selected={selectedPhases}
          onChange={setSelectedPhases}
          description="By default we draft the full SDLC. Uncheck any phases to leave them out — the AI won't generate work packages for a disabled phase."
        />

        <label className="block">
          <span className="label">Existing / proposed technologies (optional)</span>
          <textarea
            className="input min-h-[4rem]"
            value={technology}
            onChange={(e) => setTechnology(e.target.value)}
            placeholder="Languages, frameworks, cloud, datastores the client uses or plans to use — e.g. React + Node, Java/Spring, Postgres, AWS"
          />
        </label>

        <label className="block">
          <span className="label">AI tooling (optional)</span>
          <textarea
            className="input min-h-[4rem]"
            value={tooling}
            onChange={(e) => setTooling(e.target.value)}
            placeholder="e.g. Claude Code for dev, CodeRabbit for review, Figma AI for design"
          />
        </label>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={handleContinue}
            disabled={!canSubmit}
            className="btn-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            Continue
          </button>
          <a href="/wbs" className="btn-secondary">
            Cancel
          </a>
        </div>
      </section>
    </div>
  );
}

"use client";

import dynamic from "next/dynamic";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  calculateWbs,
  getWbsDraft,
  previewWbs,
  saveWbsDraft,
} from "@/lib/api-client";
import { LlmUsageModal } from "@/components/LlmUsageModal";
import { Modal } from "@/components/Modal";
import {
  CODEBASE_CONTEXT_LABELS,
  DEFAULT_ROSTER,
  ROLE_CATEGORY_LABELS,
  ROLE_SENIORITY_LABELS,
  type Stage2Input,
  type Stage3Input,
} from "@/lib/schemas";
import { formatHours, formatUSD, formatUSDPrecise } from "@/lib/format";
import type { DualScenarioEstimate, LlmUsage } from "@/lib/types";
import { designateTeamMembers } from "@/lib/team-roster";
import { clearWbsCache, loadWbsCache, saveWbsCache } from "@/lib/wbs-store";
import { countLeaves, rollupRange, type WbsTaskInput } from "@/lib/wbs";

// Client-only (MUI X Tree View + emotion) — dynamic import avoids MUI SSR setup / hydration flash.
const WbsTreeViewEditor = dynamic(
  () => import("@/components/WbsTreeViewEditor").then((m) => m.WbsTreeViewEditor),
  { ssr: false, loading: () => <p className="muted text-sm">Loading editor…</p> },
);

type SaveState = "idle" | "saving" | "saved" | "error";

// Bottom-up WBS estimates run optimistic, so the WBS flow seeds an explicit 30% contingency reserve
// (independent of the global admin contingency the quick estimate uses). The user can override it.
const WBS_DEFAULT_CONTINGENCY_PCT = 30;

export default function WbsEditorPage() {
  const router = useRouter();
  const draftId = String(useParams().draftId);

  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [projectName, setProjectName] = useState("");
  const [rawInput, setRawInput] = useState("");
  const [tree, setTree] = useState<WbsTaskInput[]>([]);
  const [stage2, setStage2] = useState<Stage2Input | undefined>();
  const [stage3, setStage3] = useState<Stage3Input | undefined>();
  const [contingency, setContingency] = useState<number>(WBS_DEFAULT_CONTINGENCY_PCT);

  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [preview, setPreview] = useState<DualScenarioEstimate | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inputsOpen, setInputsOpen] = useState(false);
  const [teamOpen, setTeamOpen] = useState(false);
  const [llmOpen, setLlmOpen] = useState(false);
  const [llmUsage, setLlmUsage] = useState<LlmUsage | null>(null);

  const roster = stage2?.roster?.roles ?? DEFAULT_ROSTER;

  // --- load / resume -----------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    async function load() {
      let draft = null;
      try {
        draft = await getWbsDraft(draftId);
        // TODO(bugfix): server wins unconditionally. If the user made offline edits
        // (autosave PUT was failing, so only loadWbsCache has them) and Neo4j later
        // returns, those edits are silently discarded here. A reliable reconcile needs
        // an `updated_at` on the locally-cached draft (autosave doesn't set one) plus
        // backend timestamp semantics — out of scope for the frontend-only WBS fix.
      } catch {
        draft = loadWbsCache(draftId); // Neo4j off / not found → fall back to localStorage
      }
      if (cancelled) return;
      if (!draft) {
        setLoadError("This draft couldn't be loaded (it may have been deleted).");
        setLoaded(true);
        return;
      }
      setProjectName(draft.project_name || "");
      setRawInput(draft.raw_input || "");
      setTree(draft.tree || []);
      setStage2(draft.stage2 ?? undefined);
      setStage3(draft.stage3 ?? undefined);
      // Resume the saved reserve; a pre-existing draft without one falls back to the WBS default.
      setContingency(draft.contingency_pct ?? WBS_DEFAULT_CONTINGENCY_PCT);
      setLlmUsage(draft.llm_usage ?? null);
      setLoaded(true);
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [draftId]);

  // --- debounced autosave ------------------------------------------------------------------
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!loaded || loadError) return;
    const draft = {
      draft_id: draftId, project_name: projectName, raw_input: rawInput, tree, stage2, stage3,
      contingency_pct: contingency,
    };
    setSaveState("saving");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      // Both the local mirror (a full-tree JSON.stringify) and the server PUT run once per typing
      // pause, not synchronously on every keystroke/render.
      saveWbsCache(draft);
      try {
        await saveWbsDraft(draftId, {
          project_name: projectName,
          raw_input: rawInput,
          tree,
          stage2,
          stage3,
          contingency_pct: contingency,
        });
        setSaveState("saved");
      } catch {
        setSaveState("error"); // kept in the local cache; the server save will retry on next edit
      }
    }, 800);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tree, projectName, contingency, loaded]);

  // --- re-evaluate / save ------------------------------------------------------------------
  const handleReevaluate = useCallback(async () => {
    setPreviewing(true);
    setError(null);
    try {
      setPreview(
        // Thread draft_id so the backend keys its Monte Carlo RNG on the SAME seed as Submit
        // (calculateWbs passes draft_id too) — otherwise preview and commit show different bands.
        await previewWbs({
          project_name: projectName,
          raw_input: rawInput,
          draft_id: draftId,
          tree,
          stage2,
          stage3,
          contingency_pct: contingency,
        }),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPreviewing(false);
    }
  }, [draftId, projectName, rawInput, tree, stage2, stage3, contingency]);

  async function handleSave() {
    // Cancel any pending debounced autosave: the commit supersedes the draft (the
    // server drops it on commit), so a late PUT firing after we navigate away would
    // re-create the just-consumed draft as a zombie.
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    setCommitting(true);
    setError(null);
    try {
      const env = await calculateWbs({
        project_name: projectName,
        raw_input: rawInput,
        draft_id: draftId,
        tree,
        stage2,
        stage3,
        contingency_pct: contingency,
      });
      clearWbsCache(draftId);
      router.push(`/estimate/${env.estimate_id}/review`);
    } catch (e) {
      setError((e as Error).message);
      setCommitting(false);
    }
  }

  // Render-body full-tree walks, memoized so they don't re-run on every render (e.g. save-state
  // transitions). Hooks stay above the early returns to keep the hook order stable.
  const localRange = useMemo(() => rollupRange(tree), [tree]);
  const leafCount = useMemo(() => countLeaves(tree), [tree]);
  // Individual team members (duplicate roles get A/B/C… designations) for the Team modal.
  const teamMembers = useMemo(() => designateTeamMembers(roster), [roster]);

  if (!loaded) return <p className="muted text-sm">Loading draft…</p>;
  if (loadError)
    return (
      <div className="space-y-3">
        <p className="text-sm text-rose-600">{loadError}</p>
        <a href="/wbs" className="btn-secondary">
          ← Back to WBS estimates
        </a>
      </div>
    );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold text-slate-900">Edit WBS</h1>
          <p className="text-xs muted">
            {leafCount} task{leafCount === 1 ? "" : "s"} · ~{Math.round(localRange.most_likely)} h
            (local estimate)
            {saveState === "saving" && " · Saving…"}
            {saveState === "saved" && " · Saved"}
            {saveState === "error" && " · Saved locally (offline)"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setInputsOpen(true)}
            title="View the original project description and AI tooling this WBS was drafted from"
            className="btn-secondary"
          >
            Project brief
          </button>
          <button
            type="button"
            onClick={() => setTeamOpen(true)}
            title="View the generated team roster used to attribute and cost this WBS"
            className="btn-secondary"
          >
            Team
          </button>
          {llmUsage && llmUsage.call_count > 0 && (
            <button
              type="button"
              onClick={() => setLlmOpen(true)}
              title="LLM token cost to draft this WBS"
              className="btn-secondary inline-flex items-center gap-1.5"
            >
              <svg
                viewBox="0 0 24 24"
                className="h-4 w-4"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <circle cx="12" cy="12" r="9" />
                <path d="M12 7v10" />
                <path d="M14.5 9.3A2.4 2 0 0 0 12 8c-1.4 0-2.5.8-2.5 1.9 0 1 .9 1.6 2.5 2s2.5 1 2.5 2.1-1.1 1.9-2.5 1.9a2.4 2 0 0 1-2.5-1.3" />
              </svg>
              {formatUSDPrecise(llmUsage.cost_usd)}
            </button>
          )}
          <button
            type="button"
            onClick={handleReevaluate}
            disabled={previewing || leafCount === 0}
            className="btn-secondary disabled:opacity-50"
          >
            {previewing ? "Re-evaluating…" : "Re-evaluate"}
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={committing || leafCount === 0}
            title="Finalize the WBS and open the full review"
            className="btn-primary disabled:opacity-50"
          >
            {committing ? "Submitting…" : "Submit"}
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-start gap-4">
        <label className="block grow">
          <span className="label">Project name</span>
          <input
            className="input max-w-md"
            value={projectName}
            onChange={(e) => setProjectName(e.target.value)}
          />
        </label>
        <label className="block">
          <span className="label">Contingency reserve</span>
          <div className="flex items-center gap-1">
            <input
              type="number"
              min={0}
              max={100}
              step={1}
              className="input w-24"
              value={contingency}
              onChange={(e) => {
                const n = Number(e.target.value);
                setContingency(Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0);
              }}
            />
            <span className="text-sm muted">%</span>
          </div>
          <span className="block text-xs muted mt-1 max-w-[16rem]">
            Buffer added to cost &amp; timeline — bottom-up estimates run optimistic. Re-evaluate to apply.
          </span>
        </label>
      </div>

      {error && <p className="text-sm text-rose-600">{error}</p>}

      {preview && (
        <section className="card space-y-1">
          <h2 className="section-title">Latest rollup</h2>
          <p className="text-sm text-slate-700">
            AI-assisted:{" "}
            <span className="font-semibold">
              {formatHours(preview.total_ai_assisted_hours.most_likely)}
            </span>{" "}
            · {formatUSD(preview.total_cost_ai_assisted_usd)}
          </p>
          <p className="text-sm text-slate-700">
            Manual-only:{" "}
            <span className="font-semibold">
              {formatHours(preview.total_manual_only_hours.most_likely)}
            </span>{" "}
            · {formatUSD(preview.total_cost_manual_only_usd)}
          </p>
          <p className="text-xs muted">
            Duration ≈ {preview.duration_weeks_low.toFixed(1)}–
            {preview.duration_weeks_high.toFixed(1)} weeks · team {preview.team_size ?? "—"}
            {preview.contingency_pct ? ` · incl. ${preview.contingency_pct}% contingency` : ""}
          </p>
        </section>
      )}

      <section className="card">
        <WbsTreeViewEditor tree={tree} roster={roster} onChange={setTree} />
      </section>

      <Modal
        open={inputsOpen}
        onClose={() => setInputsOpen(false)}
        title="Project brief"
        widthClass="max-w-2xl"
      >
        <div className="space-y-4 text-sm">
          <div>
            <h3 className="font-semibold text-slate-900">Project description</h3>
            {rawInput.trim() ? (
              <p className="mt-1 whitespace-pre-wrap text-slate-700">{rawInput}</p>
            ) : (
              <p className="mt-1 muted">No description was provided.</p>
            )}
          </div>

          <div>
            <h3 className="font-semibold text-slate-900">AI tooling</h3>
            {stage3?.ai_tooling_description?.trim() ? (
              <p className="mt-1 whitespace-pre-wrap text-slate-700">
                {stage3.ai_tooling_description}
              </p>
            ) : (
              <p className="mt-1 muted">No AI tooling was described.</p>
            )}
          </div>

          <div>
            <h3 className="font-semibold text-slate-900">Codebase context</h3>
            <p className="mt-1 text-slate-700">
              {stage3?.codebase_context
                ? CODEBASE_CONTEXT_LABELS[stage3.codebase_context]
                : "—"}
            </p>
          </div>

          {stage3?.technology_stack?.trim() && (
            <div>
              <h3 className="font-semibold text-slate-900">Technology stack</h3>
              <p className="mt-1 whitespace-pre-wrap text-slate-700">
                {stage3.technology_stack}
              </p>
            </div>
          )}
        </div>
      </Modal>

      <Modal
        open={teamOpen}
        onClose={() => setTeamOpen(false)}
        title="Team roster"
        widthClass="max-w-2xl"
      >
        {teamMembers.length === 0 ? (
          <p className="text-sm muted">No team roster was generated for this draft.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="py-2 pr-3 font-medium">Team member</th>
                  <th className="py-2 pr-3 font-medium">Category</th>
                  <th className="py-2 pr-3 font-medium">Seniority</th>
                  <th className="py-2 pr-3 text-right font-medium">Rate / h</th>
                  <th className="py-2 text-right font-medium">Allocation</th>
                </tr>
              </thead>
              <tbody>
                {teamMembers.map((member) => (
                  <tr key={member.role_id} className="border-b border-slate-100">
                    <td className="py-2 pr-3">
                      <div className="flex items-center gap-2">
                        <span
                          aria-hidden="true"
                          className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-brand-50 text-xs font-semibold text-brand-700"
                        >
                          {member.designation ?? member.description.charAt(0).toUpperCase()}
                        </span>
                        <span className="text-slate-800">{member.label}</span>
                      </div>
                    </td>
                    <td className="py-2 pr-3 text-slate-700">
                      {ROLE_CATEGORY_LABELS[member.category]}
                    </td>
                    <td className="py-2 pr-3 text-slate-700">
                      {ROLE_SENIORITY_LABELS[member.seniority]}
                    </td>
                    <td className="py-2 pr-3 text-right text-slate-700">
                      {formatUSD(member.rate_per_hour)}
                    </td>
                    <td className="py-2 text-right text-slate-700">{member.percentage}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Modal>

      {llmUsage && (
        <LlmUsageModal
          open={llmOpen}
          onClose={() => setLlmOpen(false)}
          usage={llmUsage}
          title="WBS draft — LLM cost & usage"
          subtitle="What it cost to draft this Work Breakdown Structure via the Anthropic API — the planner's token cost, separate from the project labor cost."
        />
      )}
    </div>
  );
}

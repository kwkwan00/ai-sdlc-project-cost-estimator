"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { ProgressBar } from "@/components/ProgressBar";
import { RoleRosterEditor } from "@/components/RoleRosterEditor";
import { classifyTooling, draftWbs } from "@/lib/api-client";
import { PROGRESS_TICK_MS, trickle } from "@/lib/progress";
import { proposeRoster } from "@/lib/roster-agui";
import {
  DEFAULT_ROSTER,
  NO_TOOLING,
  type CustomRoleInput,
  type PhaseTooling,
  type Stage2Input,
  type Stage3Input,
} from "@/lib/schemas";
import { draftWbsStreaming } from "@/lib/wbs-agui";
import {
  clearWbsNewDraft,
  loadWbsNewDraft,
  saveWbsCache,
  type WbsNewDraft,
} from "@/lib/wbs-store";

// Time-based progress for the "Draft WBS" action. The planner is a single opaque LLM call with no
// sub-progress to stream, so the bar trickles toward a ceiling < 100 during each phase and only the
// real response snaps it to 100. Ceilings leave headroom so 100% unambiguously means "done".
const PHASE_TOOLING_CEILING = 18; // awaiting AI-tooling classification (often already resolved)
const PHASE_DRAFT_CEILING = 90; // the planner call — the long pole
const PROGRESS_REVEAL_MS = 300; // let 100% paint before routing to the editor

function buildStage2(roles: CustomRoleInput[]): Stage2Input {
  return {
    industry: "",
    project_type: "greenfield",
    integration_count: 0,
    integration_list: [],
    engagement_model: "tm",
    regulatory_requirements: [],
    roster: { roles },
  };
}

export default function WbsTeamPage() {
  const router = useRouter();
  const [draft, setDraft] = useState<WbsNewDraft | null>(null);
  const [roster, setRoster] = useState<CustomRoleInput[]>(DEFAULT_ROSTER);
  const [rosterLoading, setRosterLoading] = useState(true);
  const [rosterError, setRosterError] = useState<string | null>(null);
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ranRef = useRef(false);
  // The in-flight (or resolved) AI-tooling classification. handleDraft awaits this so the draft
  // can't be committed with stale all-`none` tooling — which would zero every phase's AI reduction
  // (ai == manual), permanently baking $0 AI savings into the persisted draft + its review page.
  const toolingRef = useRef<Promise<PhaseTooling> | null>(null);
  // Time-based draft progress (see the ceilings above). `ceilingRef` lets the running ticker pick
  // up a phase change without restarting the interval. `latestMsgRef` holds the most recent streamed
  // status message; the ticker flushes it to the label, throttling fast event bursts to one paint
  // per tick so the text stays readable.
  const [progress, setProgress] = useState(0);
  const [phaseLabel, setPhaseLabel] = useState("");
  const ceilingRef = useRef(0);
  const latestMsgRef = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function stopTrickle() {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }

  // Enter a progress phase: set its label + ceiling and start the ticker if it isn't already running.
  // The ticker advances the bar AND flushes the latest streamed status message into the label.
  function beginPhase(label: string, ceiling: number) {
    latestMsgRef.current = null; // show the phase's own label until backend status messages arrive
    setPhaseLabel(label);
    ceilingRef.current = ceiling;
    if (!timerRef.current) {
      timerRef.current = setInterval(() => {
        setProgress((p) => trickle(p, ceilingRef.current));
        if (latestMsgRef.current !== null) setPhaseLabel(latestMsgRef.current);
      }, PROGRESS_TICK_MS);
    }
  }

  // Stop the ticker if the user navigates away mid-draft.
  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  // Load the handoff from /wbs/new; bounce back if a user deep-linked here directly.
  useEffect(() => {
    const d = loadWbsNewDraft();
    if (!d) {
      router.replace("/wbs/new");
      return;
    }
    setDraft(d);
  }, [router]);

  async function runRosterProposal(d: WbsNewDraft) {
    setRosterError(null);
    setRosterLoading(true);
    try {
      // Seed the roster proposal from the description AND the AI tooling, so the proposed
      // team reflects how the work will actually be done.
      const rawInput = d.tooling.trim()
        ? `${d.raw_input}\n\nAI tooling in use: ${d.tooling}`
        : d.raw_input;
      const result = await proposeRoster({
        stage2: buildStage2([]),
        rawInput,
        selectedPhases: d.selected_phases,
      });
      setRoster(result.roster);
      setRationale(result.rationale || "");
    } catch (e) {
      // Degrade to the default roster; surface the reason so the user can retry.
      setRoster(DEFAULT_ROSTER);
      setRationale("");
      setRosterError((e as Error)?.message || "Couldn't propose a team.");
    } finally {
      setRosterLoading(false);
    }
  }

  // Once the handoff lands: kick off AI-tooling classification (silent) + propose the roster.
  // Classification runs concurrently with the user editing the roster, but handleDraft awaits its
  // result, so an empty tooling field resolves to NO_TOOLING and a non-empty one always lands.
  useEffect(() => {
    if (!draft || ranRef.current) return;
    ranRef.current = true;
    toolingRef.current = draft.tooling.trim()
      ? classifyTooling(draft.tooling)
          .then((r) => r.ai_tooling)
          .catch(() => NO_TOOLING)
      : Promise.resolve(NO_TOOLING);
    runRosterProposal(draft);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  async function handleDraft() {
    if (!draft) return;
    setBusy(true);
    setError(null);
    setProgress(0);
    // Phase 1 — wait for the AI-tooling classification kicked off on mount (usually already resolved;
    // awaiting it rather than reading stale state stops a fast click committing all-`none` tooling).
    beginPhase("Reviewing your AI tooling…", PHASE_TOOLING_CEILING);
    try {
      const stage2 = buildStage2(roster);
      const aiTooling = await (toolingRef.current ?? Promise.resolve(NO_TOOLING));
      // Phase 2 — the planner LLM call (the long pole). No sub-progress to stream, so the bar
      // trickles toward PHASE_DRAFT_CEILING until the real response lands.
      beginPhase("Drafting the work breakdown…", PHASE_DRAFT_CEILING);
      const stage3: Stage3Input = {
        codebase_context: draft.codebase,
        ai_tooling_description: draft.tooling,
        ai_tooling: aiTooling,
        technology_stack: draft.technology ?? "",
      };
      // Stream the planner so the bar shows the actual work package being drafted (only the latest).
      // On any streaming failure (SSE/transport/RUN_ERROR) fall back to the plain POST draft so the
      // page still works — the time-based bar just carries on without live events.
      const res = await draftWbsStreaming(
        {
          rawInput: draft.raw_input,
          projectName: draft.project_name || undefined,
          stage2,
          stage3,
          selectedPhases: draft.selected_phases,
        },
        // Stash each friendly status; the trickle ticker flushes it to the label (throttled).
        { onProgress: (msg) => { latestMsgRef.current = msg; } },
      ).catch((streamErr) => {
        console.warn("[wbs] streaming draft failed; falling back to POST", streamErr);
        setPhaseLabel("Drafting the work breakdown…");
        return draftWbs({
          raw_input: draft.raw_input,
          project_name: draft.project_name || undefined,
          stage2,
          stage3,
          selected_phases: draft.selected_phases,
        });
      });
      saveWbsCache({
        draft_id: res.draft_id,
        project_name: draft.project_name,
        raw_input: draft.raw_input,
        tree: res.tree,
        stage2,
        stage3,
        llm_usage: res.llm_usage,
      });
      clearWbsNewDraft();
      // Real work done → snap to 100% and let it paint before routing to the editor.
      stopTrickle();
      setPhaseLabel("Ready");
      setProgress(100);
      await new Promise((resolve) => setTimeout(resolve, PROGRESS_REVEAL_MS));
      router.push(`/wbs/edit/${res.draft_id}`);
    } catch (e) {
      stopTrickle();
      setProgress(0);
      setError((e as Error).message);
      setBusy(false);
    }
  }

  if (!draft) return <p className="muted text-sm">Loading…</p>;

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <p className="text-xs uppercase tracking-wide muted">Step 2 of 3</p>
        <h1 className="text-2xl font-bold text-slate-900">Your team</h1>
        <p className="text-slate-600 max-w-2xl">
          We proposed a team from your description and AI tooling. Adjust the roles, rates, and
          effort split, then draft the work breakdown.
        </p>
      </div>

      <section className="card space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="section-title">Team roster</h2>
          <button
            type="button"
            onClick={() => runRosterProposal(draft)}
            disabled={rosterLoading}
            className="btn-secondary text-xs disabled:opacity-50"
          >
            {rosterLoading
              ? "Proposing…"
              : rosterError
                ? "✨ Retry proposing team"
                : "✨ Re-propose team"}
          </button>
        </div>

        {rosterLoading ? (
          <p className="muted text-sm">Proposing a team from your description…</p>
        ) : (
          <>
            {rosterError && (
              <p className="text-xs text-amber-700">
                {rosterError} — showing a default team you can edit.
              </p>
            )}
            {rationale && <p className="text-xs muted">{rationale}</p>}
            <RoleRosterEditor value={roster} onChange={setRoster} disabled={rosterLoading} />
          </>
        )}
      </section>

      {error && <p className="text-sm text-rose-600">{error}</p>}

      {busy && (
        <ProgressBar value={progress} label={phaseLabel} showPercent={false} className="max-w-md" />
      )}

      <div className="flex items-center gap-3">
        <a href="/wbs/new" className="btn-secondary">
          ← Back
        </a>
        <button
          type="button"
          onClick={handleDraft}
          disabled={busy || rosterLoading}
          className="btn-primary disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Drafting…" : "Draft WBS"}
        </button>
      </div>
    </div>
  );
}

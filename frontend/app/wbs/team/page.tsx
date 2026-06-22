"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { RoleRosterEditor } from "@/components/RoleRosterEditor";
import { classifyTooling, draftWbs } from "@/lib/api-client";
import { proposeRoster } from "@/lib/roster-agui";
import {
  DEFAULT_ROSTER,
  NO_TOOLING,
  type CustomRoleInput,
  type PhaseTooling,
  type Stage2Input,
  type Stage3Input,
} from "@/lib/schemas";
import {
  clearWbsNewDraft,
  loadWbsNewDraft,
  saveWbsCache,
  type WbsNewDraft,
} from "@/lib/wbs-store";

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
      const result = await proposeRoster({ stage2: buildStage2([]), rawInput });
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
    try {
      const stage2 = buildStage2(roster);
      // Wait for the in-flight classification (started on mount) rather than reading possibly-stale
      // state — a fast click otherwise commits all-`none` tooling and zeroes the AI savings.
      const aiTooling = await (toolingRef.current ?? Promise.resolve(NO_TOOLING));
      const stage3: Stage3Input = {
        codebase_context: draft.codebase,
        ai_tooling_description: draft.tooling,
        ai_tooling: aiTooling,
      };
      const res = await draftWbs({
        raw_input: draft.raw_input,
        project_name: draft.project_name || undefined,
        stage2,
        stage3,
      });
      saveWbsCache({
        draft_id: res.draft_id,
        project_name: draft.project_name,
        raw_input: draft.raw_input,
        tree: res.tree,
        stage2,
        stage3,
      });
      clearWbsNewDraft();
      router.push(`/wbs/edit/${res.draft_id}`);
    } catch (e) {
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

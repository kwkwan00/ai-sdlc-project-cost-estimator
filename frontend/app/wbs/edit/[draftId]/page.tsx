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
import { DEFAULT_ROSTER, type Stage2Input, type Stage3Input } from "@/lib/schemas";
import { formatHours, formatUSD } from "@/lib/format";
import type { DualScenarioEstimate } from "@/lib/types";
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
    </div>
  );
}

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  deleteWbsDraft,
  duplicateWbsDraft,
  listWbsDrafts,
} from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import type { WbsDraftSummary } from "@/lib/wbs";

export default function WbsLanding() {
  const router = useRouter();
  const [items, setItems] = useState<WbsDraftSummary[]>([]);
  const [resumable, setResumable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listWbsDrafts()
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setResumable(res.resumable);
        setError(null);
      })
      .catch((e) => !cancelled && setError((e as Error).message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [tick]);

  async function handleDuplicate(id: string) {
    setBusyId(id);
    try {
      const res = await duplicateWbsDraft(id);
      router.push(`/wbs/edit/${res.draft_id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusyId(null);
    }
  }

  async function handleDelete(it: WbsDraftSummary) {
    if (!window.confirm(`Delete draft "${it.project_name || "Untitled"}"?`)) return;
    setBusyId(it.draft_id);
    try {
      await deleteWbsDraft(it.draft_id);
      setTick((t) => t + 1);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <h1 className="text-3xl font-bold text-slate-900">WBS estimates</h1>
        <p className="text-slate-600 max-w-2xl">
          Estimate bottom-up: an AI assistant drafts a Work Breakdown Structure of tasks from your
          description, you refine the tree and effort, and the cost rolls up via Monte Carlo PERT.
        </p>
      </div>

      <div className="flex items-center gap-3">
        <Link href="/wbs/new" className="btn-primary">
          New WBS estimate
        </Link>
        <Link href="/" className="btn-secondary">
          ← Back to dashboard
        </Link>
      </div>

      <section className="card space-y-3">
        <h2 className="section-title">Resume a draft</h2>

        {error && <p className="text-sm text-rose-600">{error}</p>}

        {loading ? (
          <p className="muted text-sm">Loading…</p>
        ) : !resumable ? (
          <p className="muted text-sm">
            Draft resume needs Neo4j. With Neo4j off, a draft stays only in this browser until you
            save the estimate.
          </p>
        ) : items.length === 0 ? (
          <p className="muted text-sm">No saved drafts yet. Start a new WBS estimate above.</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {items.map((it) => (
              <li key={it.draft_id} className="flex items-center gap-1 py-2">
                <Link
                  href={`/wbs/edit/${it.draft_id}`}
                  className="block flex-1 min-w-0 rounded-md px-2 -mx-2 hover:bg-slate-50"
                >
                  <p className="font-medium text-slate-900 truncate">
                    {it.project_name || "Untitled WBS draft"}
                  </p>
                  <p className="text-xs muted">
                    {it.task_count} task{it.task_count === 1 ? "" : "s"}
                    {it.updated_at ? ` · ${formatDate(it.updated_at)}` : ""}
                  </p>
                </Link>
                <button
                  type="button"
                  onClick={() => handleDuplicate(it.draft_id)}
                  disabled={busyId !== null}
                  className="btn-secondary text-xs disabled:opacity-40"
                >
                  Duplicate
                </button>
                <button
                  type="button"
                  onClick={() => handleDelete(it)}
                  disabled={busyId !== null}
                  className="btn-secondary text-xs disabled:opacity-40"
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  deleteEstimate,
  listEstimateHistory,
  type EstimateHistoryItem,
} from "@/lib/api-client";
import { formatHours, formatUSD } from "@/lib/format";

function formatDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

const STATUS_LABEL: Record<string, string> = {
  pending: "Pending",
  pass_1_running: "Pass 1 running",
  awaiting_answers: "Awaiting answers",
  pass_2_running: "Pass 2 running",
  synthesizing: "Synthesizing",
  completed: "Completed",
  failed: "Failed",
};

const PAGE_SIZE = 10;

function TrashIcon() {
  return (
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
      <path d="M3 6h18" />
      <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6M14 11v6" />
    </svg>
  );
}

export default function Dashboard() {
  const [items, setItems] = useState<EstimateHistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0); // zero-based
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listEstimateHistory({ limit: PAGE_SIZE, offset: page * PAGE_SIZE })
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setTotal(res.total);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [page, refreshTick]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const rangeStart = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const rangeEnd = Math.min(total, page * PAGE_SIZE + items.length);

  async function handleDelete(it: EstimateHistoryItem) {
    if (
      !window.confirm(
        `Delete "${it.project_name || "this estimate"}"? This can't be undone.`,
      )
    ) {
      return;
    }
    setDeletingId(it.estimate_id);
    try {
      await deleteEstimate(it.estimate_id);
      setError(null);
      // If we just removed the only row on a page past the first, step back a page;
      // otherwise re-fetch the current page (the refreshTick bump re-runs the effect).
      if (items.length === 1 && page > 0) {
        setPage((p) => p - 1);
      } else {
        setRefreshTick((t) => t + 1);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <h1 className="text-3xl font-bold text-slate-900">Cost estimates</h1>
        <p className="text-slate-600 max-w-2xl">
          Start a new estimate to size a software project across the six SDLC
          phases — Discovery, UX/Design, Development, Code Review, Deployment, and
          QA/Testing — using six collaborative AI twins.
        </p>
      </div>

      <div className="flex items-center gap-3">
        <Link href="/estimate/new" className="btn-primary">
          New estimate
        </Link>
        <Link href="/estimate/new?quick=1" className="btn-secondary">
          Quick estimate (skip Stages 2 + 3)
        </Link>
      </div>

      <section className="card space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="section-title">Recent estimates</h2>
          {total > 0 && (
            <span className="text-xs muted">
              {rangeStart}–{rangeEnd} of {total}
            </span>
          )}
        </div>

        {error && (
          <p className="text-sm text-rose-600">
            Couldn&apos;t load history: {error}
          </p>
        )}

        {loading ? (
          <p className="muted text-sm">Loading…</p>
        ) : items.length === 0 ? (
          <p className="muted text-sm">
            No saved estimates yet. Completed estimates appear here once Postgres
            history is connected — run one and it&apos;ll show up.
          </p>
        ) : (
          <>
          <ul className="divide-y divide-slate-100">
            {items.map((it) => {
              const done = it.status === "completed";
              const row = (
                <div className="flex flex-wrap items-center justify-between gap-2 py-3">
                  <div className="min-w-0">
                    <p className="font-medium text-slate-900 truncate">
                      {it.project_name || "Untitled estimate"}
                    </p>
                    <p className="text-xs muted">
                      {STATUS_LABEL[it.status] ?? it.status}
                      {it.industry ? ` · ${it.industry}` : ""}
                      {it.updated_at ? ` · ${formatDate(it.updated_at)}` : ""}
                    </p>
                  </div>
                  {done && (
                    <div className="text-right text-xs muted">
                      <span className="font-semibold text-slate-700">
                        {formatHours(it.total_ai_assisted_hours ?? 0)}
                      </span>{" "}
                      AI ·{" "}
                      <span className="font-semibold text-slate-700">
                        {formatUSD(it.total_cost_ai_assisted_usd ?? 0)}
                      </span>
                      {it.ai_hours_saved != null && (
                        <span className="text-emerald-600">
                          {" "}
                          · saved {formatHours(it.ai_hours_saved)}
                        </span>
                      )}
                    </div>
                  )}
                </div>
              );
              return (
                <li key={it.estimate_id} className="flex items-center gap-1">
                  {done ? (
                    <Link
                      href={`/estimate/${it.estimate_id}/review`}
                      className="block flex-1 min-w-0 rounded-md px-2 -mx-2 hover:bg-slate-50"
                    >
                      {row}
                    </Link>
                  ) : (
                    <div className="flex-1 min-w-0 px-2 -mx-2 opacity-80">{row}</div>
                  )}
                  <button
                    type="button"
                    onClick={() => handleDelete(it)}
                    disabled={deletingId !== null}
                    aria-label={`Delete ${it.project_name || "estimate"}`}
                    title="Delete estimate"
                    className="shrink-0 rounded-md p-2 text-slate-400 hover:bg-rose-50 hover:text-rose-600 focus:outline-none focus:ring-2 focus:ring-rose-300 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <TrashIcon />
                  </button>
                </li>
              );
            })}
          </ul>
          {totalPages > 1 && (
            <nav
              className="flex items-center justify-between gap-2 pt-2"
              aria-label="History pages"
            >
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0 || loading}
                className="btn-secondary text-sm disabled:cursor-not-allowed disabled:opacity-40"
              >
                ← Previous
              </button>
              <span className="text-xs muted">
                Page {page + 1} of {totalPages}
              </span>
              <button
                type="button"
                onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                disabled={page >= totalPages - 1 || loading}
                className="btn-secondary text-sm disabled:cursor-not-allowed disabled:opacity-40"
              >
                Next →
              </button>
            </nav>
          )}
          </>
        )}
      </section>
    </div>
  );
}

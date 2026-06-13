"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
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

export default function Dashboard() {
  const [items, setItems] = useState<EstimateHistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listEstimateHistory()
      .then(setItems)
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

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
        <h2 className="section-title">Recent estimates</h2>

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
                <li key={it.estimate_id}>
                  {done ? (
                    <Link
                      href={`/estimate/${it.estimate_id}/review`}
                      className="block rounded-md px-2 -mx-2 hover:bg-slate-50"
                    >
                      {row}
                    </Link>
                  ) : (
                    <div className="px-2 -mx-2 opacity-80">{row}</div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}

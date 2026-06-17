"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { use, useEffect, useState } from "react";

import { StageProgress } from "@/components/StageProgress";
import { getEstimate, submitAnswers } from "@/lib/api-client";
import { questionsPollInterval } from "@/lib/estimate-status";
import { rankQuestions, totalImpact, voiLabel } from "@/lib/voi";

interface PageProps {
  params: Promise<{ id: string }>;
}

/** Badge tint per VoI tier — warmer = higher information value. */
const VOI_TONE: Record<"high" | "medium" | "low" | "none", string> = {
  high: "bg-amber-100 text-amber-800",
  medium: "bg-brand-50 text-brand-700",
  low: "bg-slate-100 text-slate-600",
  none: "bg-slate-100 text-slate-400",
};

export default function QuestionsPage({ params }: PageProps) {
  const { id } = use(params);
  const router = useRouter();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  // Set once answers are submitted so polling resumes through Pass 2 → completed.
  // (`awaiting_answers` deliberately stops polling while we wait on the user, so
  // without this the page would never see the post-submit status transitions.)
  const [resuming, setResuming] = useState(false);

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["estimate", id],
    queryFn: () => getEstimate(id),
    refetchInterval: (q) => questionsPollInterval(q.state.data?.status, resuming),
  });

  useEffect(() => {
    if (!data) return;
    if (data.status === "completed") {
      router.push(`/estimate/${id}/review`);
    }
    if (data.status === "pass_2_running" || data.status === "synthesizing") {
      // Polling completion handler will redirect once status flips to completed.
    }
  }, [data, id, router]);

  const handleSubmit = async (skip: boolean) => {
    setSubmitting(true);
    setResuming(true);
    try {
      const ans = skip ? {} : answers;
      await submitAnswers(id, ans, skip);
      // Pass 2 runs in the background; resume polling so we catch `completed`
      // (which the effect above redirects on). Kick an immediate refetch.
      await refetch();
    } catch (e) {
      setSubmitting(false);
      setResuming(false);
      alert("Failed to submit answers: " + (e as Error).message);
    }
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <StageProgress current={4} />
      <header className="space-y-2">
        <h1 className="text-2xl font-bold text-slate-900">Clarifying questions</h1>
        <p className="muted">
          The twins flagged the highest-impact ambiguities. Answer what you can —
          each question has a sensible default you can accept by leaving blank.
        </p>
      </header>

      {error && (
        <div className="rounded-md border border-rose-300 bg-rose-50 p-3 text-sm text-rose-700">
          {(error as Error).message}
        </div>
      )}

      {isLoading || !data ? (
        <div className="card">Loading...</div>
      ) : data.status === "pass_1_running" || data.status === "pending" ? (
        <div className="card flex items-center gap-3">
          <div className="h-4 w-4 rounded-full border-2 border-brand-500 border-t-transparent animate-spin" />
          <span>Pass 1 in progress — six twins running in parallel.</span>
        </div>
      ) : data.status === "pass_2_running" ||
        data.status === "synthesizing" ||
        resuming ? (
        <div className="card flex items-center gap-3">
          <div className="h-4 w-4 rounded-full border-2 border-brand-500 border-t-transparent animate-spin" />
          <span>Pass 2 in progress — refining with your answers...</span>
        </div>
      ) : data.status === "awaiting_answers" ? (
        <div className="card space-y-6">
          {data.clarifying_questions.length === 0 ? (
            <p className="muted">No questions — pass 1 produced no gaps.</p>
          ) : (
            (() => {
              const ranked = rankQuestions(data.clarifying_questions);
              const total = totalImpact(ranked);
              return (
                <>
                  <p className="rounded-md bg-brand-50 px-3 py-2 text-xs text-brand-700">
                    Ordered by potential impact — answer the highest-impact
                    questions first to tighten the estimate. The badge shows roughly
                    how many hours each gap could shift the estimate (a relative
                    value-of-information proxy, not a precise figure).
                  </p>
                  {ranked.map((q) => {
                    const badge = voiLabel(q, total);
                    return (
                      <div key={q.id}>
                        <div className="flex items-baseline justify-between gap-3">
                          <label className="label">{q.text}</label>
                          <span
                            title={
                              badge.sharePct !== undefined
                                ? `~${badge.sharePct}% of the total flagged impact`
                                : undefined
                            }
                            className={`shrink-0 whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] font-medium ${VOI_TONE[badge.level]}`}
                          >
                            {badge.text}
                          </span>
                        </div>
                        <input
                          type="text"
                          className="input mt-1"
                          placeholder={`Default: ${q.suggested_default}`}
                          value={answers[q.id] || ""}
                          onChange={(e) =>
                            setAnswers({ ...answers, [q.id]: e.target.value })
                          }
                        />
                      </div>
                    );
                  })}
                </>
              );
            })()
          )}
          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={() => handleSubmit(true)}
              disabled={submitting}
              className="btn-secondary"
            >
              Use all defaults
            </button>
            <button
              type="button"
              onClick={() => handleSubmit(false)}
              disabled={submitting}
              className="btn-primary"
            >
              Submit answers
            </button>
          </div>
        </div>
      ) : data.status === "failed" ? (
        <div className="rounded-md border border-rose-300 bg-rose-50 p-3 text-sm text-rose-700">
          Estimate failed: {data.error || "unknown error"}
        </div>
      ) : (
        <div className="card">Status: {data.status}</div>
      )}
    </div>
  );
}

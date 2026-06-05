"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { use, useEffect, useState } from "react";

import { StageProgress } from "@/components/StageProgress";
import { getEstimate, submitAnswers } from "@/lib/api-client";

interface PageProps {
  params: Promise<{ id: string }>;
}

export default function QuestionsPage({ params }: PageProps) {
  const { id } = use(params);
  const router = useRouter();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  const { data, error, isLoading } = useQuery({
    queryKey: ["estimate", id],
    queryFn: () => getEstimate(id),
    refetchInterval: (q) => {
      const env = q.state.data;
      if (!env) return 1500;
      if (env.status === "pass_1_running" || env.status === "pending") return 1500;
      return false;
    },
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
    try {
      const ans = skip ? {} : answers;
      await submitAnswers(id, ans, skip);
      // Poll until completed; the useQuery refetchInterval handles the rest.
    } catch (e) {
      setSubmitting(false);
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
      ) : data.status === "pass_2_running" || data.status === "synthesizing" ? (
        <div className="card flex items-center gap-3">
          <div className="h-4 w-4 rounded-full border-2 border-brand-500 border-t-transparent animate-spin" />
          <span>Pass 2 in progress — refining with your answers...</span>
        </div>
      ) : data.status === "awaiting_answers" ? (
        <div className="card space-y-6">
          {data.clarifying_questions.length === 0 ? (
            <p className="muted">No questions — pass 1 produced no gaps.</p>
          ) : (
            data.clarifying_questions.map((q) => (
              <div key={q.id}>
                <div className="flex items-baseline justify-between gap-3">
                  <label className="label">{q.text}</label>
                  <span className="text-xs muted">
                    impact ≈ {Math.round(q.impact_hours)}h
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
            ))
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

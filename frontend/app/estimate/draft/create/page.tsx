"use client";

import { useRouter } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { createEstimate, buildCreatePayload } from "@/lib/api-client";
import { clearDraft, loadDraft, saveSession } from "@/lib/wizard-store";

function CreateInner() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const draft = loadDraft();
      if (!draft || !draft.raw_input) {
        router.push("/estimate/new");
        return;
      }
      try {
        const payload = buildCreatePayload(
          draft.raw_input,
          draft.project_name,
          draft.stage2,
          draft.stage3
        );
        const envelope = await createEstimate(payload);
        if (cancelled) return;
        saveSession(envelope.estimate_id, draft);
        clearDraft();
        router.push(`/estimate/${envelope.estimate_id}/questions`);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  if (error) {
    return (
      <div className="card max-w-xl">
        <h2 className="section-title">Could not start estimate</h2>
        <p className="text-rose-700 text-sm mt-2">{error}</p>
        <button
          onClick={() => router.push("/estimate/new")}
          className="btn-secondary mt-4"
        >
          Back to start
        </button>
      </div>
    );
  }

  return (
    <div className="card max-w-xl flex items-center gap-3">
      <div className="h-4 w-4 rounded-full border-2 border-brand-500 border-t-transparent animate-spin" />
      <p>Starting Pass 1 — six twins running in parallel...</p>
    </div>
  );
}

export default function CreatePage() {
  return (
    <Suspense fallback={<div className="card max-w-xl">Loading...</div>}>
      <CreateInner />
    </Suspense>
  );
}

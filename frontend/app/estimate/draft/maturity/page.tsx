"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { MaturitySlider } from "@/components/MaturitySlider";
import { StageProgress } from "@/components/StageProgress";
import { buildCreatePayload, createEstimate } from "@/lib/api-client";
import type { Stage3Input } from "@/lib/schemas";
import { clearDraft, loadDraft, saveSession } from "@/lib/wizard-store";

const DEFAULT: Stage3Input = {
  discovery_maturity: 1,
  ux_design_maturity: 1,
  development_maturity: 1,
  code_review_maturity: 1,
  deployment_maturity: 1,
  qa_testing_maturity: 1,
};

export default function Stage3DraftPage() {
  const router = useRouter();
  const [stage3, setStage3] = useState<Stage3Input>(DEFAULT);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const draft = loadDraft();
    if (draft?.stage3) setStage3({ ...DEFAULT, ...draft.stage3 });
  }, []);

  const persistAndCreate = async () => {
    const draft = loadDraft();
    if (!draft) {
      router.push("/estimate/new");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const payload = buildCreatePayload(
        draft.raw_input,
        draft.project_name,
        draft.stage2,
        stage3
      );
      const envelope = await createEstimate(payload);
      saveSession(envelope.estimate_id, {
        raw_input: draft.raw_input,
        project_name: draft.project_name,
        stage2: draft.stage2,
        stage3,
      });
      clearDraft();
      router.push(`/estimate/${envelope.estimate_id}/questions`);
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <StageProgress current={3} />
      <header className="space-y-2">
        <h1 className="text-2xl font-bold text-slate-900">AI maturity by phase</h1>
        <p className="muted">
          Score AI maturity per phase. Higher levels apply larger reductions
          (capped per phase). Team composition lives in Stage 2 (Project
          context) — adjust the roster there.
        </p>
      </header>

      <div className="card space-y-4">
        <h2 className="section-title">AI maturity by phase</h2>
        <MaturitySlider
          label="Discovery"
          value={stage3.discovery_maturity}
          onChange={(v) => setStage3({ ...stage3, discovery_maturity: v })}
        />
        <MaturitySlider
          label="UX / Design"
          value={stage3.ux_design_maturity}
          onChange={(v) => setStage3({ ...stage3, ux_design_maturity: v })}
        />
        <MaturitySlider
          label="Development"
          value={stage3.development_maturity}
          onChange={(v) => setStage3({ ...stage3, development_maturity: v })}
        />
        <MaturitySlider
          label="Code Review"
          value={stage3.code_review_maturity}
          onChange={(v) => setStage3({ ...stage3, code_review_maturity: v })}
        />
        <MaturitySlider
          label="Deployment / DevOps"
          value={stage3.deployment_maturity}
          onChange={(v) => setStage3({ ...stage3, deployment_maturity: v })}
        />
        <MaturitySlider
          label="QA / Testing"
          value={stage3.qa_testing_maturity}
          onChange={(v) => setStage3({ ...stage3, qa_testing_maturity: v })}
        />
      </div>

      {error && (
        <div className="rounded-md border border-rose-300 bg-rose-50 p-3 text-sm text-rose-700">
          {error}
        </div>
      )}

      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => router.push("/estimate/draft/context")}
          className="btn-secondary"
        >
          Back
        </button>
        <button
          type="button"
          onClick={persistAndCreate}
          disabled={submitting}
          className="btn-primary"
        >
          {submitting ? "Starting Pass 1..." : "Generate estimate"}
        </button>
      </div>
    </div>
  );
}

"use client";

import type {
  CreateEstimateInput,
  Stage2Input,
  Stage3Input,
} from "./schemas";
import type { EstimateEnvelope } from "./types";

export interface Stage2Prefill {
  // Roster-free: the prefill endpoint no longer returns a team roster. The
  // roster is proposed asynchronously by the AG-UI roster agent on Stage 2, and
  // the frontend supplies its own DEFAULT_ROSTER until that snapshot lands.
  stage2: Omit<Stage2Input, "roster">;
  summary: string;
  ambiguity_score: number;
}

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function jsonFetch<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let detail: string;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      detail = await res.text();
    }
    throw new Error(`${res.status} ${res.statusText} — ${detail}`);
  }
  return res.json();
}

export async function createEstimate(
  body: CreateEstimateInput
): Promise<EstimateEnvelope> {
  return jsonFetch("/estimates", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Ask the backend to analyze the Stage 1 description and return a Stage 2
 *  partial the form can pre-populate. The backend always returns a valid
 *  response (defaults + 0.7 ambiguity when the LLM call falls back), so the
 *  only error case is a network failure — callers should treat that as
 *  "skip prefill, continue with blank Stage 2". */
export async function prefillFromDescription(
  rawInput: string
): Promise<Stage2Prefill> {
  return jsonFetch("/estimates/draft/prefill", {
    method: "POST",
    body: JSON.stringify({ raw_input: rawInput }),
  });
}

export async function getEstimate(id: string): Promise<EstimateEnvelope> {
  return jsonFetch(`/estimates/${id}`);
}

export async function submitAnswers(
  id: string,
  answers: Record<string, string>,
  skipRemaining = false
): Promise<EstimateEnvelope> {
  return jsonFetch(`/estimates/${id}/answers`, {
    method: "POST",
    body: JSON.stringify({ answers, skip_remaining: skipRemaining }),
  });
}

export function streamUrl(id: string): string {
  return `${API_BASE}/estimates/${id}/stream`;
}

/** Helper for the wizard: pack Stage 2/3 into the create payload. */
export function buildCreatePayload(
  rawInput: string,
  projectName: string | undefined,
  stage2: Stage2Input | undefined,
  stage3: Stage3Input | undefined
): CreateEstimateInput {
  return {
    raw_input: rawInput,
    project_name: projectName,
    stage2,
    stage3,
  };
}

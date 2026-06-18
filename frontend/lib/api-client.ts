"use client";

import type {
  ClassifyToolingResponse,
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
  // AI tools the description mentioned, for pre-filling the Stage 3 tooling field.
  // Empty string when none were named.
  ai_tooling_description: string;
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

/** Classify a freeform AI-tooling description into per-phase levels. The backend
 *  always returns a valid mapping (all "none" on a blank description or any
 *  LLM/MCP failure), so the only error case is a network failure — callers should
 *  treat that as "continue with no AI tooling". */
export async function classifyTooling(
  description: string
): Promise<ClassifyToolingResponse> {
  return jsonFetch("/estimates/draft/classify-tooling", {
    method: "POST",
    body: JSON.stringify({ description }),
  });
}

export interface EstimateHistoryItem {
  estimate_id: string;
  project_name: string;
  status: string;
  industry: string | null;
  project_type: string | null;
  total_ai_assisted_hours: number | null;
  total_manual_only_hours: number | null;
  ai_hours_saved: number | null;
  total_cost_ai_assisted_usd: number | null;
  confidence: number | null;
  created_at: string | null;
  updated_at: string | null;
}

/** One page of recent persisted estimates: the rows for the requested slice plus
 *  `total`, the full row count, so the dashboard can render page controls. */
export interface EstimateHistoryPage {
  items: EstimateHistoryItem[];
  total: number;
}

/** A page of recent persisted estimates for the dashboard history list, newest
 *  first. Returns an empty page (total 0) when the backend has no Postgres history
 *  configured. */
export async function listEstimateHistory(
  params: { limit?: number; offset?: number } = {},
): Promise<EstimateHistoryPage> {
  const { limit = 10, offset = 0 } = params;
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return jsonFetch(`/estimates/history?${qs}`);
}

/** Delete an estimate — removes its persisted history (+ phase rows) and any
 *  in-memory state. Idempotent: deleting a missing estimate still resolves. Uses a
 *  raw fetch (not jsonFetch) because the 204 response carries no JSON body. */
export async function deleteEstimate(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/estimates/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
}

export interface ReductionBandRow {
  phase: string;
  tooling_level: string;
  min_pct: number;
  max_pct: number;
  default_min_pct: number;
  default_max_pct: number;
  is_override: boolean;
}

export interface ReductionBandsResponse {
  editable: boolean;
  bands: ReductionBandRow[];
}

export async function getReductionBands(): Promise<ReductionBandsResponse> {
  return jsonFetch("/admin/reduction-bands");
}

export async function saveReductionBands(
  bands: {
    phase: string;
    tooling_level: string;
    min_pct: number;
    max_pct: number;
  }[]
): Promise<ReductionBandsResponse> {
  return jsonFetch("/admin/reduction-bands", {
    method: "PUT",
    body: JSON.stringify({ bands }),
  });
}

export interface StaffingCoefficientRow {
  key: string;
  value: number;
  default_value: number;
  min_value: number;
  max_value: number;
  is_override: boolean;
}

export interface StaffingCoefficientsResponse {
  editable: boolean;
  coefficients: StaffingCoefficientRow[];
}

/** Team-scaling (Brooks's Law + diminishing returns) coefficients for the Settings screen. */
export async function getStaffingCoefficients(): Promise<StaffingCoefficientsResponse> {
  return jsonFetch("/admin/staffing-coefficients");
}

export async function saveStaffingCoefficients(
  coefficients: { key: string; value: number }[]
): Promise<StaffingCoefficientsResponse> {
  return jsonFetch("/admin/staffing-coefficients", {
    method: "PUT",
    body: JSON.stringify({ coefficients }),
  });
}

export interface RateRow {
  category: string;
  seniority: string;
  rate: number;
  default_rate: number;
  is_override: boolean;
}

export interface RateCardResponse {
  editable: boolean;
  min_rate: number;
  max_rate: number;
  rates: RateRow[];
}

/** Default hourly rate card (per role category × seniority) for the Settings screen. */
export async function getDefaultRates(): Promise<RateCardResponse> {
  return jsonFetch("/admin/default-rates");
}

export async function saveDefaultRates(
  rates: { category: string; seniority: string; rate: number }[]
): Promise<RateCardResponse> {
  return jsonFetch("/admin/default-rates", {
    method: "PUT",
    body: JSON.stringify({ rates }),
  });
}

/** A single-choice twin sizing-method setting (Development, QA, …) for the Settings screen. */
export interface SizingMethodResponse {
  editable: boolean;
  method: string;
  default_method: string;
  methods: string[];
}

/** Discovery sizing method (Use Case Points ↔ FP-based analysis effort) for the Settings screen. */
export async function getDiscoverySizingMethod(): Promise<SizingMethodResponse> {
  return jsonFetch("/admin/discovery-sizing-method");
}

export async function saveDiscoverySizingMethod(
  method: string
): Promise<SizingMethodResponse> {
  return jsonFetch("/admin/discovery-sizing-method", {
    method: "PUT",
    body: JSON.stringify({ method }),
  });
}

/** Development sizing method (COCOMO II ↔ Function Points) for the Settings screen. */
export async function getDevelopmentSizingMethod(): Promise<SizingMethodResponse> {
  return jsonFetch("/admin/development-sizing-method");
}

export async function saveDevelopmentSizingMethod(
  method: string
): Promise<SizingMethodResponse> {
  return jsonFetch("/admin/development-sizing-method", {
    method: "PUT",
    body: JSON.stringify({ method }),
  });
}

/** QA/testing sizing method (TPA ↔ Test Case Point Analysis) for the Settings screen. */
export async function getQaSizingMethod(): Promise<SizingMethodResponse> {
  return jsonFetch("/admin/qa-sizing-method");
}

export async function saveQaSizingMethod(
  method: string
): Promise<SizingMethodResponse> {
  return jsonFetch("/admin/qa-sizing-method", {
    method: "PUT",
    body: JSON.stringify({ method }),
  });
}

/** Global contingency management-reserve % (uplifts final cost + timeline). */
export interface ContingencyResponse {
  editable: boolean;
  contingency_pct: number;
  default_pct: number;
  min_pct: number;
  max_pct: number;
}

export async function getContingency(): Promise<ContingencyResponse> {
  return jsonFetch("/admin/contingency");
}

export async function saveContingency(
  contingency_pct: number
): Promise<ContingencyResponse> {
  return jsonFetch("/admin/contingency", {
    method: "PUT",
    body: JSON.stringify({ contingency_pct }),
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

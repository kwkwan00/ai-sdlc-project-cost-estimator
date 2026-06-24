"use client";

/** Lightweight in-browser session store for the wizard.
 *
 * Stage 1 captures raw input + project name, then we POST to backend to get an
 * estimate_id. Stages 2 + 3 are stored client-side until the user clicks
 * "Generate estimate" on Stage 3 (or skips to it). The backend doesn't yet
 * have an update endpoint for stage2/3, so for MVP we send everything in the
 * create call; this means navigation flow is: 1 → 2 → 3 → POST → 4 → 5.
 */

import type { Phase, Stage2Input, Stage3Input } from "./schemas";

const KEY = (id: string) => `sdlc-est:${id}`;

interface WizardSession {
  raw_input: string;
  project_name?: string;
  stage2?: Stage2Input;
  stage3?: Stage3Input;
  /** Phases the user chose to estimate on the Stage 3 page. Persisted so navigating away and
   *  back doesn't silently reset the scope to all six (which would estimate more than asked). */
  selected_phases?: Phase[];
  /** Set when the Stage 2 values came from the LLM prefill endpoint so the
   *  Stage 2 page can surface a "Prefilled from your description" banner.
   *  Cleared once the user has visited Stage 2 (they own the values now). */
  stage2_prefilled?: boolean;
  /** Ambiguity score returned by the prefill — 0..1. Used to flag descriptions
   *  the LLM couldn't confidently interpret. */
  prefill_ambiguity?: number;
  /** One-paragraph summary the LLM produced from the raw input. Optional;
   *  surfaced as a small echo on Stage 2 so the user can sanity-check. */
  prefill_summary?: string;
  /** AI tools the prefill found in the Stage 1 description, if any. Seeds the
   *  Stage 3 tooling textarea on first visit (the user can still edit/clear it). */
  prefill_ai_tooling?: string;
  /** Set once the AG-UI roster agent has proposed (or attempted) a team on
   *  Stage 2, so revisiting the page doesn't re-trigger the run. */
  roster_proposed?: boolean;
}

export function loadSession(id: string): WizardSession | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(KEY(id));
  if (!raw) return null;
  try {
    return JSON.parse(raw) as WizardSession;
  } catch {
    return null;
  }
}

export function saveSession(id: string, session: WizardSession) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(KEY(id), JSON.stringify(session));
}

export function clearSession(id: string) {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(KEY(id));
}

/** A pre-create draft (used between Stage 1 typing and the POST after Stage 3). */
export const DRAFT_KEY = "sdlc-est:draft";

export function loadDraft(): WizardSession | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(DRAFT_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as WizardSession;
  } catch {
    return null;
  }
}

export function saveDraft(session: WizardSession) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(DRAFT_KEY, JSON.stringify(session));
}

export function clearDraft() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(DRAFT_KEY);
}

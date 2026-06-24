"use client";

/** Offline cache for an in-progress WBS draft, keyed by the server draft_id.
 *
 * The server (Neo4j) is the source of truth and the editor autosaves to it; this localStorage
 * mirror lets the editor render instantly on reopen and survive a transient backend/Neo4j outage
 * (resume then falls back to the cache). Cleared when the draft is committed or discarded.
 */

import type { CodebaseContext } from "./schemas";
import type { WbsDraft } from "./wbs";

const KEY = (id: string) => `sdlc-wbs:${id}`;

/** Pre-draft handoff between the WBS "new" page (description + tooling + codebase) and the
 *  team-roster transition page, before a server draft_id exists. */
const NEW_KEY = "sdlc-wbs:new";

export interface WbsNewDraft {
  project_name: string;
  raw_input: string;
  tooling: string;
  codebase: CodebaseContext;
  /** Existing/proposed technologies (estimation signal → stage3.technology_stack). */
  technology?: string;
}

export function loadWbsNewDraft(): WbsNewDraft | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(NEW_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as WbsNewDraft;
  } catch {
    return null;
  }
}

export function saveWbsNewDraft(draft: WbsNewDraft) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(NEW_KEY, JSON.stringify(draft));
}

export function clearWbsNewDraft() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(NEW_KEY);
}

export function loadWbsCache(draftId: string): WbsDraft | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(KEY(draftId));
  if (!raw) return null;
  try {
    return JSON.parse(raw) as WbsDraft;
  } catch {
    return null;
  }
}

export function saveWbsCache(draft: WbsDraft) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(KEY(draft.draft_id), JSON.stringify(draft));
}

export function clearWbsCache(draftId: string) {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(KEY(draftId));
}

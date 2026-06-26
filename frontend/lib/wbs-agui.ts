"use client";

import { HttpAgent, type AgentSubscriber } from "@ag-ui/client";

import type { Phase, Stage2Input, Stage3Input } from "./schemas";
import type { LlmUsage } from "./types";
import type { WbsDraftResponse, WbsTaskInput } from "./wbs";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface WbsDraftStreamArgs {
  rawInput: string;
  projectName?: string;
  stage2: Stage2Input;
  stage3: Stage3Input;
  /** SDLC phases in scope; a strict subset scopes the drafted tree (omitted ⇒ full lifecycle). */
  selectedPhases?: Phase[];
}

/** Map an AG-UI STATE_SNAPSHOT payload into a `WbsDraftResponse`.
 *
 *  Returns null when the snapshot is missing/malformed (no `draft_id`) so callers can fall back to
 *  the plain POST draft. The tree is already validated + persisted server-side, so this is a thin
 *  shape map (not a re-validation). Pure + exported for testing.
 */
export function snapshotToWbsDraft(raw: unknown): WbsDraftResponse | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const draftId = typeof obj.draft_id === "string" ? obj.draft_id : "";
  if (!draftId) return null;
  return {
    draft_id: draftId,
    tree: Array.isArray(obj.tree) ? (obj.tree as WbsTaskInput[]) : [],
    notes: typeof obj.notes === "string" ? obj.notes : "",
    llm_usage: (obj.llm_usage ?? null) as LlmUsage | null,
  };
}

/** Draft a WBS via the AG-UI streaming endpoint.
 *
 *  Invokes `onProgress(message)` with each friendly status the planner emits (reviewing → drafting
 *  packages/tasks → finalizing; the caller shows only the most recent), and resolves with the
 *  persisted draft once the STATE_SNAPSHOT lands. Throws on RUN_ERROR / transport failure / a missing
 *  snapshot so the caller can fall back to the plain POST `draftWbs` — the streamed events are a
 *  progress enhancement, never the only way to draft.
 */
export async function draftWbsStreaming(
  args: WbsDraftStreamArgs,
  handlers: { onProgress?: (message: string) => void } = {},
): Promise<WbsDraftResponse> {
  const url = `${API_BASE}/wbs/draft/agui`;
  const agent = new HttpAgent({
    url,
    // Pass a bound fetch — @ag-ui/client otherwise calls window.fetch unbound, which throws
    // "Illegal invocation" in the browser (see roster-agui.ts for the same guard).
    fetch: (input: string, init: RequestInit) => fetch(input, init),
  });

  let snapshot: unknown = null;
  let runErrorMessage: string | null = null;
  const subscriber: AgentSubscriber = {
    onCustomEvent: ({ event }) => {
      if (event?.name !== "wbs_progress") return;
      const v = (event.value ?? {}) as { message?: unknown };
      const message = typeof v.message === "string" ? v.message : "";
      if (message) handlers.onProgress?.(message);
    },
    onStateSnapshotEvent: ({ event }) => {
      snapshot = event.snapshot;
    },
    onRunErrorEvent: ({ event }) => {
      runErrorMessage = event?.message ?? "run error";
    },
  };

  let transportError: unknown = null;
  try {
    await agent.runAgent(
      {
        forwardedProps: {
          raw_input: args.rawInput,
          project_name: args.projectName ?? "",
          stage2: args.stage2,
          stage3: args.stage3,
          selected_phases: args.selectedPhases ?? [],
        },
      },
      subscriber,
    );
  } catch (e) {
    transportError = e;
  }

  // AG-UI also applies STATE_SNAPSHOT onto agent.state; prefer the captured event.
  const result = snapshotToWbsDraft(snapshot ?? agent.state);
  if (result) return result;

  const reason =
    runErrorMessage ??
    (transportError instanceof Error
      ? transportError.message
      : transportError != null
        ? String(transportError)
        : "no WBS draft snapshot received");
  throw new Error(reason);
}

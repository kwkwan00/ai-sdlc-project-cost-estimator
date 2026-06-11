"use client";

import { HttpAgent, type AgentSubscriber } from "@ag-ui/client";

import {
  roleRosterSchema,
  type CustomRoleInput,
  type Stage2Input,
} from "./schemas";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface RosterPlanItem {
  workstream: string;
  summary: string;
}

export interface RosterProposalResult {
  roster: CustomRoleInput[];
  projectPlan: RosterPlanItem[];
  rationale: string;
}

/** Map an AG-UI STATE_SNAPSHOT payload into a validated roster proposal.
 *
 *  Returns null when the snapshot is missing/malformed or the roster fails the
 *  same zod invariant the editor enforces (unique ids, percentages summing to
 *  100) — callers then keep the default roster. Pure + exported for testing.
 */
export function snapshotToRoster(raw: unknown): RosterProposalResult | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;

  const rosterObj = obj.roster as { roles?: unknown } | undefined;
  if (!rosterObj || !Array.isArray(rosterObj.roles)) return null;
  const parsed = roleRosterSchema.safeParse({ roles: rosterObj.roles });
  if (!parsed.success) return null;

  const planRaw = Array.isArray(obj.project_plan) ? obj.project_plan : [];
  const projectPlan: RosterPlanItem[] = planRaw
    .filter((p): p is Record<string, unknown> => !!p && typeof p === "object")
    .map((p) => ({
      workstream: String(p.workstream ?? ""),
      summary: String(p.summary ?? ""),
    }))
    .filter((p) => p.workstream.length > 0);

  const rationale =
    typeof obj.staffing_rationale === "string" ? obj.staffing_rationale : "";

  return { roster: parsed.data.roles, projectPlan, rationale };
}

/** Run the AG-UI roster agent and return the proposed roster + plan + rationale.
 *
 *  Streams under the hood (RUN_STARTED → STATE_SNAPSHOT → RUN_FINISHED); we
 *  resolve once the snapshot lands. Throws on RUN_ERROR / transport failure /
 *  malformed snapshot so the caller can fall back to the default roster.
 */
export async function proposeRoster(args: {
  stage2: Stage2Input;
  rawInput: string;
}): Promise<RosterProposalResult> {
  const url = `${API_BASE}/estimates/draft/roster/agui`;
  const agent = new HttpAgent({
    url,
    // @ag-ui/client invokes its stored `fetch` unbound, which throws
    // "TypeError: Failed to execute 'fetch' on 'Window': Illegal invocation" in
    // browsers (window.fetch requires this === window). Node's fetch tolerates an
    // unbound call, so it only fails in the browser. Pass a bound wrapper.
    fetch: (input: string, init: RequestInit) => fetch(input, init),
  });

  let snapshot: unknown = null;
  let runErrorMessage: string | null = null;
  const subscriber: AgentSubscriber = {
    onStateSnapshotEvent: ({ event }) => {
      snapshot = event.snapshot;
    },
    onRunErrorEvent: ({ event }) => {
      // Backend emitted RUN_ERROR (e.g. the roster agent raised, or the model
      // call failed) — capture the message so we can surface the real reason.
      runErrorMessage = event?.message ?? "run error";
    },
  };

  console.info("[roster-agui] requesting roster proposal", { url });

  let transportError: unknown = null;
  try {
    await agent.runAgent(
      { forwardedProps: { stage2: args.stage2, raw_input: args.rawInput } },
      subscriber
    );
  } catch (e) {
    // Transport-level failure (unreachable backend, CORS, network). Capture it
    // rather than swallow it — it's almost always the actual cause.
    transportError = e;
  }

  // AG-UI applies STATE_SNAPSHOT to agent.state too; prefer the captured event.
  const result = snapshotToRoster(snapshot ?? agent.state);
  if (result) {
    console.info("[roster-agui] proposal applied", {
      roles: result.roster.length,
      planItems: result.projectPlan.length,
    });
    return result;
  }

  // No usable snapshot — throw with the most specific reason we have so the UI
  // shows it (a backend RUN_ERROR message, or a transport "Failed to fetch")
  // instead of a generic placeholder, and log full detail to the console.
  const reason =
    runErrorMessage ??
    (transportError instanceof Error
      ? transportError.message
      : transportError != null
        ? String(transportError)
        : "no roster snapshot received");
  console.error("[roster-agui] proposal failed", { url, runErrorMessage, transportError });
  throw new Error(reason);
}

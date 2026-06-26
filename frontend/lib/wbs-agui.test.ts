import { describe, expect, it, vi } from "vitest";

// Stub the AG-UI client so importing the module under test doesn't pull the real HttpAgent
// (network/rxjs). The capturing stub also lets us assert how draftWbsStreaming builds the run.
const agui = vi.hoisted(() => ({
  lastConfig: null as { fetch?: unknown } | null,
  lastRunInput: null as { forwardedProps?: Record<string, unknown> } | null,
}));
vi.mock("@ag-ui/client", () => ({
  HttpAgent: class {
    state: unknown = undefined;
    constructor(config: { fetch?: unknown }) {
      agui.lastConfig = config;
    }
    async runAgent(input: { forwardedProps?: Record<string, unknown> }) {
      agui.lastRunInput = input;
      return { result: null, newMessages: [] };
    }
  },
}));

import { draftWbsStreaming, snapshotToWbsDraft } from "./wbs-agui";
import type { Stage2Input, Stage3Input } from "./schemas";

describe("snapshotToWbsDraft", () => {
  it("maps a valid snapshot to a WbsDraftResponse", () => {
    const out = snapshotToWbsDraft({
      draft_id: "d1",
      tree: [{ id: "p1", name: "Build", children: [] }],
      notes: "drafted",
      llm_usage: { call_count: 1, cost_usd: 0.4, by_model: [] },
    });
    expect(out).not.toBeNull();
    expect(out!.draft_id).toBe("d1");
    expect(out!.tree).toHaveLength(1);
    expect(out!.notes).toBe("drafted");
    expect(out!.llm_usage?.cost_usd).toBe(0.4);
  });

  it("defaults tree/notes/llm_usage when absent", () => {
    const out = snapshotToWbsDraft({ draft_id: "d2" });
    expect(out).not.toBeNull();
    expect(out!.tree).toEqual([]);
    expect(out!.notes).toBe("");
    expect(out!.llm_usage).toBeNull();
  });

  it("returns null without a draft_id (caller then falls back to the POST draft)", () => {
    expect(snapshotToWbsDraft({ tree: [] })).toBeNull();
    expect(snapshotToWbsDraft({ draft_id: 123 })).toBeNull();
  });

  it("returns null for non-object input", () => {
    expect(snapshotToWbsDraft(null)).toBeNull();
    expect(snapshotToWbsDraft(undefined)).toBeNull();
    expect(snapshotToWbsDraft("nope")).toBeNull();
  });
});

describe("draftWbsStreaming", () => {
  it("passes a bound fetch to HttpAgent (guards browser 'Illegal invocation')", async () => {
    // The stub yields no STATE_SNAPSHOT, so the call rejects — we only assert a fetch fn was handed
    // to the HttpAgent constructor (otherwise @ag-ui/client calls window.fetch unbound and throws).
    await expect(
      draftWbsStreaming({
        rawInput: "x",
        stage2: {} as unknown as Stage2Input,
        stage3: {} as unknown as Stage3Input,
      }),
    ).rejects.toThrow();
    expect(typeof agui.lastConfig?.fetch).toBe("function");
  });

  it("forwards the draft inputs (incl. selected phases) as forwardedProps", async () => {
    await expect(
      draftWbsStreaming({
        rawInput: "build a portal",
        projectName: "Portal",
        stage2: {} as unknown as Stage2Input,
        stage3: {} as unknown as Stage3Input,
        selectedPhases: ["development", "qa_testing"],
      }),
    ).rejects.toThrow(); // no snapshot from the stub → rejects; we assert the forwarded props
    const props = agui.lastRunInput?.forwardedProps;
    expect(props?.raw_input).toBe("build a portal");
    expect(props?.project_name).toBe("Portal");
    expect(props?.selected_phases).toEqual(["development", "qa_testing"]);
  });

  it("forwards an empty phase list when none is given (full-scope, no constraint)", async () => {
    await expect(
      draftWbsStreaming({
        rawInput: "x",
        stage2: {} as unknown as Stage2Input,
        stage3: {} as unknown as Stage3Input,
      }),
    ).rejects.toThrow();
    expect(agui.lastRunInput?.forwardedProps?.selected_phases).toEqual([]);
  });
});

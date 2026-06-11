import { describe, expect, it, vi } from "vitest";

// Stub the AG-UI client so importing the module under test doesn't pull the real
// HttpAgent (network/rxjs). The capturing stub also lets us assert how
// proposeRoster constructs the agent (e.g. that it passes a bound fetch).
const agui = vi.hoisted(() => ({ lastConfig: null as { fetch?: unknown } | null }));
vi.mock("@ag-ui/client", () => ({
  HttpAgent: class {
    state: unknown = undefined;
    constructor(config: { fetch?: unknown }) {
      agui.lastConfig = config;
    }
    async runAgent() {
      return { result: null, newMessages: [] };
    }
  },
}));

import { proposeRoster, snapshotToRoster } from "./roster-agui";
import type { Stage2Input } from "./schemas";

const role = (id: string, pct: number) => ({
  role_id: id,
  description: "Some role",
  category: "engineering",
  seniority: "senior",
  rate_per_hour: 200,
  percentage: pct,
});

describe("snapshotToRoster", () => {
  it("maps a valid snapshot to roster + plan + rationale", () => {
    const out = snapshotToRoster({
      roster: { roles: [role("a", 60), role("b", 40)] },
      project_plan: [
        { workstream: "Build", summary: "core" },
        { workstream: "QA", summary: "" },
      ],
      staffing_rationale: "Lean team",
    });
    expect(out).not.toBeNull();
    expect(out!.roster.map((r) => r.role_id)).toEqual(["a", "b"]);
    expect(out!.projectPlan).toEqual([
      { workstream: "Build", summary: "core" },
      { workstream: "QA", summary: "" },
    ]);
    expect(out!.rationale).toBe("Lean team");
  });

  it("defaults plan + rationale when absent", () => {
    const out = snapshotToRoster({ roster: { roles: [role("a", 100)] } });
    expect(out).not.toBeNull();
    expect(out!.projectPlan).toEqual([]);
    expect(out!.rationale).toBe("");
  });

  it("drops plan items without a workstream", () => {
    const out = snapshotToRoster({
      roster: { roles: [role("a", 100)] },
      project_plan: [
        { workstream: "", summary: "x" },
        { workstream: "Build", summary: "y" },
      ],
    });
    expect(out!.projectPlan).toEqual([{ workstream: "Build", summary: "y" }]);
  });

  it("returns null when the roster is missing or malformed", () => {
    expect(snapshotToRoster({ project_plan: [] })).toBeNull();
    expect(snapshotToRoster({ roster: {} })).toBeNull();
    expect(snapshotToRoster({ roster: { roles: "nope" } })).toBeNull();
  });

  it("returns null when percentages don't sum to 100 (editor invariant)", () => {
    expect(
      snapshotToRoster({ roster: { roles: [role("a", 60), role("b", 30)] } })
    ).toBeNull();
  });

  it("returns null on duplicate role_ids", () => {
    expect(
      snapshotToRoster({ roster: { roles: [role("a", 50), role("a", 50)] } })
    ).toBeNull();
  });

  it("returns null for non-object input", () => {
    expect(snapshotToRoster(null)).toBeNull();
    expect(snapshotToRoster(undefined)).toBeNull();
    expect(snapshotToRoster("nope")).toBeNull();
  });
});

describe("proposeRoster", () => {
  it("passes a bound fetch to HttpAgent (guards browser 'Illegal invocation')", async () => {
    // The stubbed runAgent yields no STATE_SNAPSHOT, so the call rejects — we
    // only care that a fetch function was handed to the HttpAgent constructor,
    // since @ag-ui/client otherwise calls window.fetch unbound and throws.
    await expect(
      proposeRoster({ stage2: {} as unknown as Stage2Input, rawInput: "x" })
    ).rejects.toThrow();
    expect(typeof agui.lastConfig?.fetch).toBe("function");
  });
});

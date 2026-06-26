import { describe, expect, it } from "vitest";

import type { RoleHeadcount } from "./types";
import { deriveWbsSchedule } from "./wbs-schedule";
import type { WbsTaskInput } from "./wbs";

function leaf(
  id: string,
  roleId: string,
  hours: number,
  depends_on: string[] = [],
  phase = "development" as const,
): WbsTaskInput {
  return {
    id,
    name: id,
    phase,
    role_id: roleId,
    optimistic: hours,
    most_likely: hours,
    pessimistic: hours,
    depends_on,
    children: [],
  };
}

function pkg(id: string, children: WbsTaskInput[], depends_on: string[] = []): WbsTaskInput {
  return { id, name: id, depends_on, children };
}

function member(role_id: string, headcount = 1, role_description = role_id): RoleHeadcount {
  return {
    role_id,
    role_description,
    category: "engineering",
    seniority: "senior",
    headcount,
    rate_per_hour: 100,
    ai_assisted_hours: 0,
    manual_only_hours: 0,
    ai_assisted_cost_usd: 0,
    manual_only_cost_usd: 0,
  };
}

describe("deriveWbsSchedule — dependencies", () => {
  it("sequences a task after its predecessor finishes", () => {
    const tree = [pkg("p", [leaf("a", "eng", 40), leaf("b", "eng2", 40, ["a"])])];
    const { tasks } = deriveWbsSchedule(tree, [member("eng"), member("eng2")]);
    const a = tasks.find((t) => t.id === "a")!;
    const b = tasks.find((t) => t.id === "b")!;
    expect(b.startWeek).toBeGreaterThanOrEqual(a.endWeek - 1e-9);
  });

  it("runs independent tasks on different members in parallel (overlapping)", () => {
    const tree = [pkg("p", [leaf("a", "eng", 40), leaf("b", "design", 40)])];
    const { tasks, totalWeeks } = deriveWbsSchedule(tree, [member("eng"), member("design")]);
    const a = tasks.find((t) => t.id === "a")!;
    const b = tasks.find((t) => t.id === "b")!;
    // Both start at 0 (no deps, different members) → makespan is one task, not two.
    expect(a.startWeek).toBeCloseTo(0);
    expect(b.startWeek).toBeCloseTo(0);
    expect(totalWeeks).toBeCloseTo(1);
  });
});

describe("deriveWbsSchedule — resource leveling by member", () => {
  it("serializes two tasks assigned to the same single-capacity member", () => {
    const tree = [pkg("p", [leaf("a", "eng", 40), leaf("b", "eng", 40)])];
    const { tasks, totalWeeks } = deriveWbsSchedule(tree, [member("eng", 1)]);
    const a = tasks.find((t) => t.id === "a")!;
    const b = tasks.find((t) => t.id === "b")!;
    // One member, capacity 1 → the two tasks can't overlap.
    const overlap = Math.min(a.endWeek, b.endWeek) - Math.max(a.startWeek, b.startWeek);
    expect(overlap).toBeLessThanOrEqual(1e-9);
    expect(totalWeeks).toBeCloseTo(2);
    expect(new Set(tasks.map((t) => t.slot))).toEqual(new Set([0])); // capacity 1 → one slot
  });

  it("parallelizes same-role tasks when the member has headcount > 1", () => {
    const tree = [pkg("p", [leaf("a", "eng", 40), leaf("b", "eng", 40)])];
    const { tasks, totalWeeks } = deriveWbsSchedule(tree, [member("eng", 2)]);
    // Capacity 2 → both run at once on separate slots → makespan one task long.
    expect(totalWeeks).toBeCloseTo(1);
    expect(new Set(tasks.map((t) => t.slot))).toEqual(new Set([0, 1]));
  });
});

describe("deriveWbsSchedule — package dependencies", () => {
  it("makes every successor-package task wait on the predecessor package", () => {
    const tree = [
      pkg("p1", [leaf("a", "eng", 40)]),
      pkg("p2", [leaf("b", "design", 40)], ["p1"]),
    ];
    const { tasks } = deriveWbsSchedule(tree, [member("eng"), member("design")]);
    const a = tasks.find((t) => t.id === "a")!;
    const b = tasks.find((t) => t.id === "b")!;
    // Different members, but the package dep forces b after a.
    expect(b.startWeek).toBeGreaterThanOrEqual(a.endWeek - 1e-9);
    expect(b.deps).toContain("a");
  });
});

describe("deriveWbsSchedule — critical chain + scaling", () => {
  it("marks the dependency chain that determines the makespan as critical", () => {
    const tree = [
      pkg("p", [
        leaf("a", "eng", 40),
        leaf("b", "eng2", 40, ["a"]),
        leaf("c", "eng3", 8), // short, parallel, off the critical chain
      ]),
    ];
    const { tasks, criticalPath } = deriveWbsSchedule(tree, [
      member("eng"),
      member("eng2"),
      member("eng3"),
    ]);
    expect(criticalPath).toEqual(["a", "b"]);
    expect(tasks.find((t) => t.id === "c")!.isCritical).toBe(false);
  });

  it("scales the makespan to the reported duration", () => {
    const tree = [pkg("p", [leaf("a", "eng", 40), leaf("b", "eng", 40)])]; // raw makespan 2 wk
    const { totalWeeks, tasks } = deriveWbsSchedule(tree, [member("eng", 1)], { nominalWeeks: 10 });
    expect(totalWeeks).toBeCloseTo(10);
    // proportions preserved: the second task ends at the scaled makespan.
    expect(Math.max(...tasks.map((t) => t.endWeek))).toBeCloseTo(10);
  });
});

describe("deriveWbsSchedule — Gantt rows + PERT edges", () => {
  it("groups tasks into member-swimlane rows", () => {
    const tree = [pkg("p", [leaf("a", "eng", 40), leaf("b", "design", 40)])];
    const { rows } = deriveWbsSchedule(tree, [member("eng"), member("design")]);
    expect(rows).toHaveLength(2); // one lane per member
    expect(rows.every((r) => r.firstOfMember)).toBe(true);
  });

  it("transitively reduces redundant dependency edges for the PERT network", () => {
    // a→b→c plus a redundant a→c; the reduced edge set drops a→c.
    const tree = [
      pkg("p", [
        leaf("a", "eng", 40),
        leaf("b", "eng2", 40, ["a"]),
        leaf("c", "eng3", 40, ["a", "b"]),
      ]),
    ];
    const { edges } = deriveWbsSchedule(tree, [member("eng"), member("eng2"), member("eng3")]);
    const pairs = edges.map((e) => `${e.from}->${e.to}`).sort();
    expect(pairs).toEqual(["a->b", "b->c"]); // a->c removed as redundant
  });

  it("returns an empty schedule for an empty tree", () => {
    expect(deriveWbsSchedule([], [])).toEqual({
      tasks: [],
      rows: [],
      edges: [],
      criticalPath: [],
      totalWeeks: 0,
    });
  });
});

describe("deriveWbsSchedule — member labels", () => {
  it("disambiguates duplicate role descriptions with A/B designations", () => {
    const tree = [pkg("p", [leaf("a", "e1", 40), leaf("b", "e2", 40)])];
    const { tasks } = deriveWbsSchedule(tree, [
      member("e1", 1, "Senior Engineer"),
      member("e2", 1, "Senior Engineer"),
    ]);
    const labels = tasks.map((t) => t.memberLabel).sort();
    expect(labels).toEqual(["Senior Engineer A", "Senior Engineer B"]);
  });
});

describe("deriveWbsSchedule — cycle safety (review fix #0)", () => {
  it("breaks a dependency cycle into a DAG instead of hanging or placing a task at week 0", () => {
    const tree = [pkg("p", [leaf("a", "eng", 40, ["b"]), leaf("b", "eng2", 40, ["a"])])];
    const { tasks, totalWeeks } = deriveWbsSchedule(tree, [member("eng"), member("eng2")]);
    expect(tasks).toHaveLength(2);
    expect(Number.isFinite(totalWeeks)).toBe(true);
    // Exactly one of the two edges survives, so the two tasks are ordered (don't overlap) rather
    // than one starting at week 0 ahead of its predecessor.
    const [a, b] = ["a", "b"].map((id) => tasks.find((t) => t.id === id)!);
    const overlap = Math.min(a.endWeek, b.endWeek) - Math.max(a.startWeek, b.startWeek);
    expect(overlap).toBeLessThanOrEqual(1e-9);
  });
});

describe("deriveWbsSchedule — capacity cap (review fix #14)", () => {
  it("does not throw on a pathologically large headcount", () => {
    const tree = [pkg("p", [leaf("a", "eng", 40)])];
    expect(() => deriveWbsSchedule(tree, [member("eng", 100000)])).not.toThrow();
  });
});

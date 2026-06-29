import { describe, expect, it } from "vitest";

import type { MissingTask } from "./types";
import {
  addChild,
  addMissingTask,
  branchIds,
  clampHours,
  countLeaves,
  dependencyTargets,
  effectiveLeafDeps,
  findNode,
  isEmptyBranch,
  isLeaf,
  moveNode,
  moveTargets,
  newLeaf,
  newPackage,
  PHASE_ORDER,
  pertMean,
  pruneDanglingDependencies,
  removeNode,
  memberCountMap,
  rolledCostMap,
  rolledHoursMap,
  rollupRange,
  scaleLeafHoursByPhase,
  subtreeIds,
  subtreeMostLikely,
  updateNode,
  type WbsTaskInput,
} from "./wbs";

function leaf(id: string, o: number, m: number, p: number): WbsTaskInput {
  return { id, name: id, phase: "development", role_id: "sr_engineer", optimistic: o, most_likely: m, pessimistic: p, children: [] };
}

const TREE: WbsTaskInput[] = [
  { id: "pkg1", name: "Build", children: [leaf("l1", 10, 20, 40), leaf("l2", 5, 10, 20)] },
  { id: "pkg2", name: "Test", children: [leaf("l3", 4, 8, 16)] },
];

describe("scaleLeafHoursByPhase", () => {
  it("scales each leaf's 3-point hours by its phase's factor, leaving others untouched", () => {
    const tree: WbsTaskInput[] = [
      { id: "p", name: "p", children: [
        { id: "dev", name: "dev", phase: "development", role_id: "r", optimistic: 10, most_likely: 20, pessimistic: 40, children: [] },
        { id: "qa", name: "qa", phase: "qa_testing", role_id: "r", optimistic: 4, most_likely: 8, pessimistic: 16, children: [] },
      ] },
    ];
    const out = scaleLeafHoursByPhase(tree, { development: 2 });
    const dev = out[0].children[0];
    const qa = out[0].children[1];
    expect([dev.optimistic, dev.most_likely, dev.pessimistic]).toEqual([20, 40, 80]); // ×2
    expect([qa.optimistic, qa.most_likely, qa.pessimistic]).toEqual([4, 8, 16]); // untouched (no factor)
    expect(dev.optimistic! <= dev.most_likely! && dev.most_likely! <= dev.pessimistic!).toBe(true);
  });

  it("preserves structure, ids, roles, and dependencies", () => {
    const tree: WbsTaskInput[] = [
      { id: "p", name: "p", children: [
        { id: "a", name: "a", phase: "development", role_id: "sr", optimistic: 10, most_likely: 20, pessimistic: 30, depends_on: ["b"], children: [] },
      ] },
    ];
    const out = scaleLeafHoursByPhase(tree, { development: 1.5 });
    const a = out[0].children[0];
    expect(a.id).toBe("a");
    expect(a.role_id).toBe("sr");
    expect(a.depends_on).toEqual(["b"]);
    expect(a.most_likely).toBe(30); // 20 × 1.5
  });

  it("rounds to 1 decimal and is a no-op when no phase factor applies", () => {
    const tree: WbsTaskInput[] = [
      { id: "x", name: "x", phase: "development", role_id: "r", optimistic: 8, most_likely: 16, pessimistic: 32, children: [] },
    ];
    expect(scaleLeafHoursByPhase(tree, { qa_testing: 2 })[0].most_likely).toBe(16); // unaffected phase
    expect(scaleLeafHoursByPhase(tree, { development: 1.85 })[0].most_likely).toBe(29.6); // 16 × 1.85
  });
});

describe("addMissingTask", () => {
  const missing: MissingTask = {
    phase: "deployment",
    title: "HIPAA audit logging",
    rationale: "Regulated PHI access must be logged.",
    optimistic: 20,
    most_likely: 40,
    pessimistic: 80,
  };

  it("creates a 'Recommended additions' package with the suggestion as a leaf on first add", () => {
    const out = addMissingTask([], missing, "sr_engineer");
    expect(out).toHaveLength(1);
    expect(out[0].name).toBe("Recommended additions");
    const leaf = out[0].children[0];
    expect(leaf.name).toBe("HIPAA audit logging");
    expect(leaf.phase).toBe("deployment");
    expect(leaf.role_id).toBe("sr_engineer");
    expect([leaf.optimistic, leaf.most_likely, leaf.pessimistic]).toEqual([20, 40, 80]);
  });

  it("appends to the existing additions package on later adds (never a second one)", () => {
    const first = addMissingTask([], missing, "r");
    const second = addMissingTask(first, { ...missing, title: "PHI encryption" }, "r");
    const pkgs = second.filter((n) => n.name === "Recommended additions");
    expect(pkgs).toHaveLength(1);
    expect(pkgs[0].children.map((c) => c.name)).toEqual(["HIPAA audit logging", "PHI encryption"]);
  });

  it("preserves the existing tree", () => {
    const out = addMissingTask([{ id: "p", name: "Build", children: [] }], missing, "r");
    expect(out[0].name).toBe("Build");
    expect(out[1].name).toBe("Recommended additions");
  });
});

describe("pertMean", () => {
  it("weights the mode 4x", () => {
    expect(pertMean(10, 20, 40)).toBeCloseTo((10 + 80 + 40) / 6);
  });
});

describe("isLeaf", () => {
  it("distinguishes branches from leaves", () => {
    expect(isLeaf(leaf("l", 1, 2, 3))).toBe(true);
    expect(isLeaf(TREE[0])).toBe(false);
  });
});

describe("rollupRange", () => {
  it("sums leaf three-point bands across the whole tree", () => {
    expect(rollupRange(TREE)).toEqual({ optimistic: 19, most_likely: 38, pessimistic: 76 });
  });
});

describe("subtreeMostLikely", () => {
  it("sums a branch's leaf most-likely hours", () => {
    expect(subtreeMostLikely(TREE[0])).toBe(30);
    expect(subtreeMostLikely(TREE[1])).toBe(8);
  });
});

describe("countLeaves", () => {
  it("counts only leaves", () => {
    expect(countLeaves(TREE)).toBe(3);
  });
});

describe("rolledHoursMap", () => {
  it("computes every node's rolled-up most-likely hours in one pass", () => {
    const map = rolledHoursMap(TREE);
    // leaves carry their own most_likely
    expect(map.get("l1")).toBe(20);
    expect(map.get("l2")).toBe(10);
    expect(map.get("l3")).toBe(8);
    // branches sum their subtree, matching subtreeMostLikely
    expect(map.get("pkg1")).toBe(30);
    expect(map.get("pkg2")).toBe(8);
  });

  it("rolls up nested branches (branch-of-branches) correctly", () => {
    const nested: WbsTaskInput[] = [
      {
        id: "top",
        name: "Top",
        children: [
          { id: "mid", name: "Mid", children: [leaf("a", 1, 4, 9), leaf("b", 2, 6, 10)] },
          leaf("c", 3, 5, 7),
        ],
      },
    ];
    const map = rolledHoursMap(nested);
    expect(map.get("mid")).toBe(10); // 4 + 6
    expect(map.get("top")).toBe(15); // 10 + 5
    // agrees with the recursive reference
    expect(map.get("top")).toBe(subtreeMostLikely(nested[0]));
  });

  it("treats null/absent most_likely as 0", () => {
    const nullish: WbsTaskInput = {
      id: "n", name: "n", phase: "development", role_id: "r",
      optimistic: null, most_likely: null, pessimistic: null, children: [],
    };
    expect(rolledHoursMap([nullish]).get("n")).toBe(0);
  });

  it("returns an empty map for an empty tree", () => {
    expect(rolledHoursMap([]).size).toBe(0);
  });
});

describe("branchIds", () => {
  it("returns only branch (non-leaf) ids, depth-first", () => {
    expect(branchIds(TREE)).toEqual(["pkg1", "pkg2"]);
  });
  it("includes nested branch ids and excludes all leaves", () => {
    const nested: WbsTaskInput[] = [
      { id: "top", name: "Top", children: [{ id: "mid", name: "Mid", children: [leaf("x", 1, 2, 3)] }] },
    ];
    expect(branchIds(nested)).toEqual(["top", "mid"]);
  });
  it("is empty when the tree is all leaves", () => {
    expect(branchIds([leaf("l", 1, 2, 3)])).toEqual([]);
  });
});

describe("PHASE_ORDER", () => {
  it("lists the six SDLC phases in canonical order", () => {
    expect(PHASE_ORDER).toEqual([
      "discovery",
      "ux_design",
      "development",
      "code_review",
      "deployment",
      "qa_testing",
    ]);
    expect(PHASE_ORDER).toHaveLength(6);
  });
});

describe("isEmptyBranch", () => {
  it("flags a work package whose last task was removed (no children, no phase)", () => {
    expect(isEmptyBranch({ id: "pkg", name: "Empty", children: [] })).toBe(true);
  });
  it("does NOT flag a real leaf (it carries a phase)", () => {
    expect(isEmptyBranch(leaf("l", 1, 2, 3))).toBe(false);
  });
  it("does NOT flag a populated branch", () => {
    expect(isEmptyBranch(TREE[0])).toBe(false);
  });
});

describe("clampHours", () => {
  it("parses a valid non-negative number", () => {
    expect(clampHours("8")).toBe(8);
    expect(clampHours("0")).toBe(0);
    expect(clampHours("12.5")).toBe(12.5);
  });
  it("coerces empty / non-numeric / NaN to 0", () => {
    expect(clampHours("")).toBe(0);
    expect(clampHours("abc")).toBe(0);
  });
  it("clamps negatives up to 0", () => {
    expect(clampHours("-5")).toBe(0);
  });
});

describe("rollup edge cases", () => {
  const allBranches: WbsTaskInput[] = [
    { id: "top", name: "Top", children: [{ id: "mid", name: "Mid", children: [leaf("x", 2, 4, 8)] }] },
  ];
  it("rolls up a branch whose children are all branches", () => {
    expect(rollupRange(allBranches)).toEqual({ optimistic: 2, most_likely: 4, pessimistic: 8 });
    expect(subtreeMostLikely(allBranches[0])).toBe(4);
  });
  it("treats null/absent hour fields as 0", () => {
    const nullish: WbsTaskInput = {
      id: "n", name: "n", phase: "development", role_id: "r",
      optimistic: null, most_likely: null, pessimistic: null, children: [],
    };
    expect(rollupRange([nullish])).toEqual({ optimistic: 0, most_likely: 0, pessimistic: 0 });
    expect(subtreeMostLikely(nullish)).toBe(0);
  });
});

describe("newLeaf", () => {
  it("produces a valid, costable leaf with a fresh id", () => {
    const a = newLeaf("development", "sr_engineer");
    const b = newLeaf("development", "sr_engineer");
    expect(a.id).not.toEqual(b.id);
    expect(isLeaf(a)).toBe(true);
    expect(a.optimistic).toBeLessThanOrEqual(a.most_likely!);
    expect(a.most_likely!).toBeLessThanOrEqual(a.pessimistic!);
  });
});

describe("tree edits", () => {
  it("findNode locates nested nodes", () => {
    expect(findNode(TREE, "l3")?.name).toBe("l3");
    expect(findNode(TREE, "nope")).toBeNull();
  });

  it("updateNode immutably patches one node", () => {
    const next = updateNode(TREE, "l1", { most_likely: 99 });
    expect(findNode(next, "l1")?.most_likely).toBe(99);
    expect(findNode(TREE, "l1")?.most_likely).toBe(20); // original untouched
  });

  it("removeNode prunes a package emptied by deleting its last task", () => {
    const next = removeNode(TREE, "l3"); // l3 is pkg2's only child
    expect(findNode(next, "pkg2")).toBeNull(); // package pruned
    expect(findNode(next, "pkg1")).not.toBeNull(); // sibling kept
  });

  it("addChild appends to a branch and converts a leaf parent (clearing leaf fields)", () => {
    const child = newLeaf("qa_testing", "sr_engineer");
    const onBranch = addChild(TREE, "pkg1", child);
    expect(findNode(onBranch, "pkg1")?.children.map((c) => c.id)).toContain(child.id);
    // leaf → branch: leaf fields cleared so the node stays backend-valid
    const onLeaf = addChild(TREE, "l1", newLeaf("development", "sr_engineer"));
    const l1 = findNode(onLeaf, "l1")!;
    expect(l1.children.length).toBe(1);
    expect(l1.phase ?? null).toBeNull();
    expect(l1.most_likely ?? null).toBeNull();
  });

  it("subtreeIds collects a node + its descendants", () => {
    expect(subtreeIds(TREE[0])).toEqual(new Set(["pkg1", "l1", "l2"]));
  });

  it("moveNode reparents a leaf to another package", () => {
    const next = moveNode(TREE, "l3", "pkg1");
    expect(findNode(next, "pkg1")?.children.map((c) => c.id)).toContain("l3");
    expect(findNode(next, "pkg2")).toBeNull(); // pkg2 emptied → pruned
  });

  it("moveNode to top level lifts a node to the root", () => {
    const next = moveNode(TREE, "l1", null);
    expect(next.some((n) => n.id === "l1")).toBe(true);
  });

  it("moveNode refuses to move a node into its own descendant", () => {
    const next = moveNode(TREE, "pkg1", "l1"); // l1 is inside pkg1
    expect(next).toBe(TREE); // no-op (same reference)
  });

  it("moveTargets excludes the node itself, its descendants, and all leaves", () => {
    const targets = moveTargets(TREE, "l1").map((t) => t.id);
    expect(targets).toEqual(["pkg1", "pkg2"]); // both packages are valid targets for a leaf
    const pkgTargets = moveTargets(TREE, "pkg1").map((t) => t.id);
    expect(pkgTargets).toEqual(["pkg2"]); // pkg1 (self) excluded
  });

  it("newPackage seeds a branch with one blank task", () => {
    const pkg = newPackage("development", "sr_engineer");
    expect(isLeaf(pkg)).toBe(false);
    expect(pkg.children.length).toBe(1);
    expect(isLeaf(pkg.children[0])).toBe(true);
  });
});

describe("dependencyTargets", () => {
  it("offers only same-kind nodes for a leaf (other tasks, never packages)", () => {
    const ids = dependencyTargets(TREE, "l1").map((t) => t.id);
    expect(ids).toEqual(["l2", "l3"]); // self excluded; packages excluded
  });

  it("offers only same-kind nodes for a work package (other packages, never tasks)", () => {
    const ids = dependencyTargets(TREE, "pkg1").map((t) => t.id);
    expect(ids).toEqual(["pkg2"]); // self excluded; leaves excluded
  });

  it("excludes nodes that would form a cycle (a node already depending on this one)", () => {
    // l2 depends on l1, so l1 may NOT depend on l2 (that closes a 2-cycle).
    const tree: WbsTaskInput[] = [
      { id: "pkg1", name: "Build", children: [leaf("l1", 1, 2, 3), { ...leaf("l2", 1, 2, 3), depends_on: ["l1"] }] },
    ];
    expect(dependencyTargets(tree, "l1").map((t) => t.id)).toEqual([]);
    // ...but the reverse direction is still allowed.
    expect(dependencyTargets(tree, "l2").map((t) => t.id)).toEqual(["l1"]);
  });

  it("excludes transitive-cycle nodes (l1→l3 would close l1→l2→l3→l1)", () => {
    const tree: WbsTaskInput[] = [
      {
        id: "pkg1",
        name: "Build",
        children: [
          { ...leaf("l1", 1, 2, 3), depends_on: ["l2"] },
          { ...leaf("l2", 1, 2, 3), depends_on: ["l3"] },
          leaf("l3", 1, 2, 3),
        ],
      },
    ];
    // l3 already (transitively) feeds l1, so l3 must not appear as a predecessor option for l1.
    expect(dependencyTargets(tree, "l3").map((t) => t.id)).toEqual([]);
  });

  it("returns [] for an unknown id", () => {
    expect(dependencyTargets(TREE, "nope")).toEqual([]);
  });
});

describe("pruneDanglingDependencies", () => {
  it("drops references to ids no longer in the tree", () => {
    const tree: WbsTaskInput[] = [
      { id: "pkg1", name: "Build", children: [{ ...leaf("l1", 1, 2, 3), depends_on: ["gone", "l2"] }, leaf("l2", 1, 2, 3)] },
    ];
    const pruned = pruneDanglingDependencies(tree);
    expect(findNode(pruned, "l1")?.depends_on).toEqual(["l2"]); // "gone" scrubbed, "l2" kept
  });

  it("scrubs predecessor edges left dangling after a delete", () => {
    const tree: WbsTaskInput[] = [
      { id: "pkg1", name: "Build", children: [leaf("l1", 1, 2, 3), { ...leaf("l2", 1, 2, 3), depends_on: ["l1"] }] },
    ];
    const pruned = pruneDanglingDependencies(removeNode(tree, "l1"));
    expect(findNode(pruned, "l2")?.depends_on).toEqual([]); // l1 is gone, so the edge is removed
  });

  it("leaves a node without dependencies untouched", () => {
    const pruned = pruneDanglingDependencies(TREE);
    expect(findNode(pruned, "l1")?.depends_on).toBeUndefined();
  });
});

describe("rolledCostMap", () => {
  const rates = new Map([["sr_engineer", 100]]);

  it("computes leaf cost = most-likely hours × rate and rolls packages up (manual scenario)", () => {
    const cost = rolledCostMap(TREE, rates);
    expect(cost.get("l1")).toBe(2000); // 20h × $100
    expect(cost.get("l2")).toBe(1000);
    expect(cost.get("l3")).toBe(800);
    expect(cost.get("pkg1")).toBe(3000); // 2000 + 1000
    expect(cost.get("pkg2")).toBe(800);
  });

  it("discounts by the phase reduction in the AI-assisted scenario", () => {
    const cost = rolledCostMap(TREE, rates, {
      aiAssisted: true,
      reductionByPhase: { development: 50 },
    });
    expect(cost.get("l1")).toBe(1000); // 20h × $100 × (1 − 0.5)
    expect(cost.get("pkg1")).toBe(1500);
  });

  it("treats a role with no known rate as $0", () => {
    const cost = rolledCostMap(TREE, new Map()); // empty rate card
    expect(cost.get("l1")).toBe(0);
    expect(cost.get("pkg1")).toBe(0);
  });
});

describe("memberCountMap", () => {
  it("counts distinct members per node (leaf=its own, branch=union of descendants)", () => {
    const tree: WbsTaskInput[] = [
      {
        id: "pkg",
        name: "Build",
        children: [
          { ...leaf("a", 1, 2, 3), role_id: "eng" },
          { ...leaf("b", 1, 2, 3), role_id: "design" },
          { ...leaf("c", 1, 2, 3), role_id: "eng" }, // same member as a
        ],
      },
    ];
    const counts = memberCountMap(tree);
    expect(counts.get("a")).toBe(1);
    expect(counts.get("pkg")).toBe(2); // eng + design (c shares eng)
  });
});

describe("rolledCostMap — fallback rate (review fix #6)", () => {
  it("charges an unknown role_id at the fallback rate, not $0", () => {
    const tree: WbsTaskInput[] = [{ ...leaf("l1", 1, 10, 20), role_id: "ghost", children: [] }];
    expect(rolledCostMap(tree, new Map()).get("l1")).toBe(0); // no fallback → 0 (back-compat)
    expect(rolledCostMap(tree, new Map(), { fallbackRate: 150 }).get("l1")).toBe(1500); // 10h × 150
  });
});

describe("memberCountMap — headcount-aware (review fix #7)", () => {
  it("counts people (Σ headcount of distinct roles), matching the Gantt's lanes", () => {
    const tree: WbsTaskInput[] = [
      { id: "pkg", name: "Build", children: [{ ...leaf("a", 1, 2, 3), role_id: "eng", children: [] }] },
    ];
    // One role with headcount 3 → "3 members" (was "1") so it agrees with 3 Gantt swimlanes.
    expect(memberCountMap(tree, new Map([["eng", 3]])).get("pkg")).toBe(3);
    expect(memberCountMap(tree).get("pkg")).toBe(1); // unknown/absent headcount → 1
  });
});

describe("pruneDanglingDependencies — cross-kind (review fix #4)", () => {
  it("drops a depends_on that became cross-kind after a leaf→branch flip", () => {
    // l1 (leaf) depends on "x"; x is a BRANCH (cross-kind) → must be pruned, not just dangling ids.
    const tree: WbsTaskInput[] = [
      { ...leaf("l1", 1, 2, 3), depends_on: ["x"], children: [] },
      { id: "x", name: "Pkg", children: [leaf("l2", 1, 2, 3)] },
    ];
    expect(findNode(pruneDanglingDependencies(tree), "l1")?.depends_on).toEqual([]);
  });
});

describe("effectiveLeafDeps + dependencyTargets — package-implied cycles (review fix #1)", () => {
  // Package P1 depends_on P2 ⇒ every P1 leaf waits on every P2 leaf.
  const tree: WbsTaskInput[] = [
    { id: "p2", name: "P2", children: [leaf("b", 1, 2, 3)] },
    { id: "p1", name: "P1", depends_on: ["p2"], children: [leaf("a", 1, 2, 3)] },
  ];

  it("expands package deps into leaf orderings", () => {
    expect(effectiveLeafDeps(tree).get("a")).toEqual(["b"]); // a (in P1) waits on b (in P2)
  });

  it("does NOT offer a P1 leaf as a predecessor for a P2 leaf (would close a cycle)", () => {
    // 'a' already (transitively) depends on 'b' via the package edge, so 'b' may not depend on 'a'.
    expect(dependencyTargets(tree, "b").map((t) => t.id)).toEqual([]);
    // ...and the reverse direction is fine: 'a' may still depend on 'b'.
    expect(dependencyTargets(tree, "a").map((t) => t.id)).toEqual(["b"]);
  });
});

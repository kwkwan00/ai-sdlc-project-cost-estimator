import { describe, expect, it } from "vitest";

import {
  addChild,
  branchIds,
  clampHours,
  countLeaves,
  findNode,
  isEmptyBranch,
  isLeaf,
  moveNode,
  moveTargets,
  newLeaf,
  newPackage,
  PHASE_ORDER,
  pertMean,
  removeNode,
  rolledHoursMap,
  rollupRange,
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

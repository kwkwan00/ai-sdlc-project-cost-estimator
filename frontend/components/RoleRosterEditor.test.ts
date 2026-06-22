import { describe, expect, it } from "vitest";

import type { CustomRoleInput } from "@/lib/schemas";
import {
  addRoleFromCatalog,
  addRow,
  clampPercentage,
  normalizeShares,
  rebalanceOnEdit,
  removeRow,
} from "./RoleRosterEditor";

const START: CustomRoleInput[] = [
  {
    role_id: "sr_product",
    description: "Senior product manager",
    category: "product",
    seniority: "senior",
    rate_per_hour: 220,
    percentage: 20,
  },
  {
    role_id: "jr_product",
    description: "Junior product manager",
    category: "product",
    seniority: "junior",
    rate_per_hour: 140,
    percentage: 10,
  },
  {
    role_id: "sr_eng",
    description: "Senior software engineer",
    category: "engineering",
    seniority: "senior",
    rate_per_hour: 240,
    percentage: 50,
  },
  {
    role_id: "jr_eng",
    description: "Junior software engineer",
    category: "engineering",
    seniority: "junior",
    rate_per_hour: 150,
    percentage: 20,
  },
];

const sum = (rows: CustomRoleInput[]) =>
  rows.reduce((a, r) => a + r.percentage, 0);

// ---------- clampPercentage ----------

describe("clampPercentage", () => {
  it("truncates fractional values to whole integers", () => {
    expect(clampPercentage(42.7)).toBe(42);
    expect(clampPercentage(0.99)).toBe(0);
    expect(clampPercentage(99.999)).toBe(99);
  });

  it("clamps values above 100 down to 100", () => {
    expect(clampPercentage(101)).toBe(100);
    expect(clampPercentage(9999)).toBe(100);
  });

  it("clamps negative values up to 0", () => {
    expect(clampPercentage(-1)).toBe(0);
    expect(clampPercentage(-9999)).toBe(0);
  });

  it("parses numeric strings (the native event.target.value type)", () => {
    expect(clampPercentage("25")).toBe(25);
    expect(clampPercentage("25.9")).toBe(25);
    expect(clampPercentage("")).toBe(0); // empty string → NaN → 0
    expect(clampPercentage("abc")).toBe(0);
  });

  it("treats NaN / Infinity / null-ish as 0", () => {
    expect(clampPercentage(Number.NaN)).toBe(0);
    expect(clampPercentage(Number.POSITIVE_INFINITY)).toBe(0);
    expect(clampPercentage(Number.NEGATIVE_INFINITY)).toBe(0);
  });
});

// ---------- rebalanceOnEdit ----------

describe("rebalanceOnEdit", () => {
  it("preserves the sum=100 invariant after a single-row commit", () => {
    const next = rebalanceOnEdit(START, 0, 40);
    expect(sum(next)).toBe(100);
  });

  it("pins the changed row to the committed value", () => {
    const next = rebalanceOnEdit(START, 2, 30);
    expect(next[2].percentage).toBe(30);
    expect(next[2].role_id).toBe("sr_eng");
  });

  it("redistributes the freed share proportionally across the other rows", () => {
    // Others 20:10:20 → ratios 2:1:2. After sr_eng=0, the other 100 splits
    // accordingly; sr_product and jr_eng (same starting share) should match.
    const next = rebalanceOnEdit(START, 2, 0);
    expect(next[0].percentage).toBeGreaterThan(next[1].percentage);
    expect(next[0].percentage).toBe(next[3].percentage);
  });

  it("splits the remainder evenly when all other rows are zero", () => {
    const oneHotted: CustomRoleInput[] = [
      { ...START[0], percentage: 100 },
      { ...START[1], percentage: 0 },
      { ...START[2], percentage: 0 },
      { ...START[3], percentage: 0 },
    ];
    const next = rebalanceOnEdit(oneHotted, 0, 40);
    expect(next[0].percentage).toBe(40);
    expect(next[1].percentage).toBe(20);
    expect(next[2].percentage).toBe(20);
    expect(next[3].percentage).toBe(20);
  });

  it("zeroes the other rows when the changed row commits to 100", () => {
    const next = rebalanceOnEdit(START, 0, 100);
    expect(next[0].percentage).toBe(100);
    expect(next[1].percentage).toBe(0);
    expect(next[2].percentage).toBe(0);
    expect(next[3].percentage).toBe(0);
  });

  it("clamps the committed value above 100 down to 100", () => {
    const next = rebalanceOnEdit(START, 0, 9999);
    expect(next[0].percentage).toBe(100);
    expect(sum(next)).toBe(100);
  });

  it("clamps a negative committed value up to 0", () => {
    const next = rebalanceOnEdit(START, 0, -50);
    expect(next[0].percentage).toBe(0);
    expect(sum(next)).toBe(100);
  });

  it("collapses to 100 for a single-row roster regardless of input", () => {
    const single: CustomRoleInput[] = [{ ...START[0], percentage: 73 }];
    const next = rebalanceOnEdit(single, 0, 25);
    expect(next).toHaveLength(1);
    expect(next[0].percentage).toBe(100);
  });

  it("preserves tags + rate + description on non-changed rows", () => {
    const next = rebalanceOnEdit(START, 0, 40);
    expect(next[1].rate_per_hour).toBe(START[1].rate_per_hour);
    expect(next[1].category).toBe(START[1].category);
    expect(next[2].seniority).toBe(START[2].seniority);
    expect(next[1].description).toBe(START[1].description);
  });
});

// ---------- addRoleFromCatalog ----------

describe("addRoleFromCatalog", () => {
  const entry = {
    role_id: "principal_architect",
    label: "Principal Architect",
    category: "engineering",
    seniority: "senior",
    rate: 300,
  };

  it("prefills the new row from the catalog entry (label, tags, rate) and keeps sum=100", () => {
    const next = addRoleFromCatalog(START, entry);
    expect(sum(next)).toBe(100);
    expect(next).toHaveLength(START.length + 1);
    const added = next[next.length - 1];
    expect(added.description).toBe("Principal Architect");
    expect(added.category).toBe("engineering");
    expect(added.seniority).toBe("senior");
    expect(added.rate_per_hour).toBe(300);
  });

  it("assigns a fresh unique role_id (not the catalog's) and steals up to 10%", () => {
    const next = addRoleFromCatalog(START, entry);
    const ids = next.map((r) => r.role_id);
    expect(new Set(ids).size).toBe(ids.length); // all unique
    expect(next[next.length - 1].percentage).toBe(10);
  });

  it("clamps an unknown catalog category/seniority to 'other' instead of injecting it", () => {
    const rogue = { ...entry, category: "quantum", seniority: "wizard" };
    const added = addRoleFromCatalog(START, rogue)[START.length];
    expect(added.category).toBe("other");
    expect(added.seniority).toBe("other");
    expect(added.description).toBe("Principal Architect"); // label + rate still applied
    expect(added.rate_per_hour).toBe(300);
  });
});

// ---------- addRow ----------

describe("addRow", () => {
  it("preserves the sum=100 invariant when adding to a balanced roster", () => {
    const next = addRow(START);
    expect(sum(next)).toBe(100);
    expect(next).toHaveLength(START.length + 1);
  });

  it("steals 10% from the largest existing row", () => {
    const next = addRow(START);
    const largestBefore = START.reduce((best, r) => (r.percentage > best.percentage ? r : best));
    const sameRoleAfter = next.find((r) => r.role_id === largestBefore.role_id);
    expect(sameRoleAfter!.percentage).toBe(largestBefore.percentage - 10);
    expect(next[next.length - 1].percentage).toBe(10);
  });

  it("steals less than 10% if the largest row has less than 10 to give", () => {
    const tight: CustomRoleInput[] = [
      { ...START[0], percentage: 95 },
      { ...START[1], percentage: 3 },
      { ...START[2], percentage: 1 },
      { ...START[3], percentage: 1 },
    ];
    const next = addRow(tight);
    expect(sum(next)).toBe(100);
    expect(next[next.length - 1].percentage).toBeLessThanOrEqual(10);
  });

  it("seeds 100 onto the new row when starting from an empty roster", () => {
    const next = addRow([]);
    expect(next).toHaveLength(1);
    expect(next[0].percentage).toBe(100);
  });

  it("generates a unique role_id for the new row", () => {
    const next = addRow(START);
    const ids = next.map((r) => r.role_id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("seeds the new row with a non-empty description placeholder", () => {
    const next = addRow(START);
    expect(next[next.length - 1].description.trim()).not.toBe("");
  });
});

// ---------- removeRow ----------

describe("removeRow", () => {
  it("preserves the sum=100 invariant", () => {
    const next = removeRow(START, 2);
    expect(sum(next)).toBe(100);
    expect(next).toHaveLength(START.length - 1);
  });

  it("redistributes the removed share proportionally across remaining rows", () => {
    // Remove sr_eng (50%). Remaining 20:10:20 (ratio 2:1:2) splits the 50.
    const next = removeRow(START, 2);
    const byId = Object.fromEntries(next.map((r) => [r.role_id, r.percentage]));
    // sr_product (20) + jr_eng (20) should be roughly equal post-redistribution.
    expect(byId.sr_product).toBe(byId.jr_eng);
    expect(byId.sr_product).toBeGreaterThan(byId.jr_product);
  });

  it("is a no-op when only one row remains", () => {
    const single = [START[0]];
    expect(removeRow(single, 0)).toBe(single);
  });

  it("distributes evenly when all remaining rows are zero", () => {
    const oneHotted: CustomRoleInput[] = [
      { ...START[0], percentage: 100 },
      { ...START[1], percentage: 0 },
      { ...START[2], percentage: 0 },
      { ...START[3], percentage: 0 },
    ];
    const next = removeRow(oneHotted, 0);
    expect(sum(next)).toBe(100);
    // 3 rows splitting 100 evenly (rounded) → 34/33/33 with drift on the first.
    expect(next.every((r) => r.percentage >= 33)).toBe(true);
  });

  it("preserves tags and rates on remaining rows", () => {
    const next = removeRow(START, 0);
    expect(next[0].role_id).toBe("jr_product");
    expect(next[0].category).toBe("product");
    expect(next[0].rate_per_hour).toBe(140);
  });
});

// ---------- normalizeShares (the "Auto-adjust to 100%" button) ----------

describe("normalizeShares", () => {
  it("is a no-op shape when shares already total 100", () => {
    const next = normalizeShares(START);
    expect(sum(next)).toBe(100);
    expect(next.map((r) => r.percentage)).toEqual([20, 10, 50, 20]);
  });

  it("scales an over-100 roster proportionally back to 100", () => {
    const over = START.map((r) => ({ ...r, percentage: r.percentage * 2 })); // sums to 200
    const next = normalizeShares(over);
    expect(sum(next)).toBe(100);
    // Proportions preserved: original 20/10/50/20 → 20/10/50/20.
    expect(next.map((r) => r.percentage)).toEqual([20, 10, 50, 20]);
  });

  it("scales an under-100 roster up to 100", () => {
    const under: CustomRoleInput[] = [
      { ...START[0], percentage: 10 },
      { ...START[1], percentage: 10 },
      { ...START[2], percentage: 20 },
    ]; // sums to 40
    const next = normalizeShares(under);
    expect(sum(next)).toBe(100);
    expect(next.map((r) => r.percentage)).toEqual([25, 25, 50]);
  });

  it("splits evenly when all rows are zero (no division by zero)", () => {
    const zeroed = START.map((r) => ({ ...r, percentage: 0 }));
    const next = normalizeShares(zeroed);
    expect(sum(next)).toBe(100);
    expect(next.map((r) => r.percentage)).toEqual([25, 25, 25, 25]);
  });

  it("absorbs rounding drift so the sum is exactly 100", () => {
    const three: CustomRoleInput[] = [
      { ...START[0], percentage: 1 },
      { ...START[1], percentage: 1 },
      { ...START[2], percentage: 1 },
    ];
    const next = normalizeShares(three); // 33.33 each → 33/33/34
    expect(sum(next)).toBe(100);
  });

  it("collapses to 100 for a single-row roster", () => {
    expect(normalizeShares([{ ...START[0], percentage: 7 }])[0].percentage).toBe(100);
  });

  it("preserves tags, rates, and descriptions", () => {
    const next = normalizeShares(START.map((r) => ({ ...r, percentage: r.percentage * 3 })));
    expect(next[2].role_id).toBe("sr_eng");
    expect(next[2].category).toBe("engineering");
    expect(next[2].rate_per_hour).toBe(240);
  });
});

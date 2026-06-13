import { describe, expect, it } from "vitest";

import { questionsPollInterval } from "./estimate-status";

describe("questionsPollInterval", () => {
  it("polls while the estimate is in any in-progress state", () => {
    for (const s of ["pending", "pass_1_running", "pass_2_running", "synthesizing"]) {
      expect(questionsPollInterval(s, false)).toBe(1500);
    }
  });

  it("stops polling at terminal states", () => {
    expect(questionsPollInterval("completed", false)).toBe(false);
    expect(questionsPollInterval("failed", false)).toBe(false);
    // still terminal even if a resume was flagged
    expect(questionsPollInterval("completed", true)).toBe(false);
  });

  it("does NOT poll while awaiting answers (waiting on the user)", () => {
    expect(questionsPollInterval("awaiting_answers", false)).toBe(false);
  });

  it("RESUMES polling once answers are submitted (the bug fix)", () => {
    // After submit the status is briefly still awaiting_answers; resuming keeps the
    // poll alive so the page catches Pass 2 → completed and can redirect.
    expect(questionsPollInterval("awaiting_answers", true)).toBe(1500);
  });

  it("polls when status is unknown/undefined rather than getting stuck", () => {
    expect(questionsPollInterval(undefined, false)).toBe(1500);
    expect(questionsPollInterval("something_new", false)).toBe(1500);
  });
});

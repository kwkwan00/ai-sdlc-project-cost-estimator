import { describe, expect, it } from "vitest";

import { replaceSection, sowFilename } from "./sow";
import type { SowDocument } from "./types";

describe("sowFilename", () => {
  it("appends ' - SOW.docx' and keeps safe characters", () => {
    expect(sowFilename("Patient Portal")).toBe("Patient Portal - SOW.docx");
  });

  it("strips unsafe characters", () => {
    expect(sowFilename("Acme/Bank: Q1 *2026*")).toBe("AcmeBank Q1 2026 - SOW.docx");
  });

  it("collapses whitespace left by stripped characters", () => {
    // "&" is removed, leaving a double space that must collapse to one.
    expect(sowFilename("Fintech onboarding & payments")).toBe(
      "Fintech onboarding payments - SOW.docx"
    );
  });

  it("falls back to 'estimate' when empty after sanitization", () => {
    expect(sowFilename("")).toBe("estimate - SOW.docx");
    expect(sowFilename("///")).toBe("estimate - SOW.docx");
  });

  it("caps the base length at 80 chars", () => {
    const base = sowFilename("x".repeat(200)).replace(" - SOW.docx", "");
    expect(base.length).toBe(80);
  });
});

describe("replaceSection", () => {
  const doc: SowDocument = {
    estimate_id: "e1",
    template_id: "default_sow",
    title: "SOW",
    project_name: "P",
    scenario: "ai_assisted",
    placeholders: [],
    sections: [
      { id: "a", heading: "A", kind: "paragraph", text: "old", bullets: [], table: null, signatories: [], editable: true },
      { id: "b", heading: "B", kind: "bullets", text: "", bullets: ["x"], table: null, signatories: [], editable: true },
    ],
  };

  it("immutably patches the matching section only", () => {
    const next = replaceSection(doc, "a", { text: "new" });
    expect(next).not.toBe(doc);
    expect(next.sections[0].text).toBe("new");
    expect(next.sections[1]).toBe(doc.sections[1]); // untouched
    expect(doc.sections[0].text).toBe("old"); // original unchanged
  });
});

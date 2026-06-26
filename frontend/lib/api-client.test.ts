import { describe, expect, it } from "vitest";

import { buildCreatePayload } from "./api-client";

describe("buildCreatePayload — selected_phases", () => {
  it("omits selected_phases (undefined) when no subset is given — a full-scope request", () => {
    const payload = buildCreatePayload("Build an internal tool.", "Proj", undefined, undefined);
    expect(payload.selected_phases).toBeUndefined();
    expect(payload.raw_input).toBe("Build an internal tool.");
    expect(payload.project_name).toBe("Proj");
  });

  it("passes a chosen phase subset through verbatim", () => {
    const payload = buildCreatePayload(
      "Build an internal tool.",
      undefined,
      undefined,
      undefined,
      ["development", "qa_testing"]
    );
    expect(payload.selected_phases).toEqual(["development", "qa_testing"]);
  });
});

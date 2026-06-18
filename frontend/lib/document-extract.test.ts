import { describe, expect, it } from "vitest";

import {
  ACCEPTED_EXTENSIONS,
  DocumentExtractError,
  MAX_FILE_BYTES,
  extractDocumentText,
  isSupported,
} from "./document-extract";

function file(content: BlobPart, name: string, type = "text/plain"): File {
  return new File([content], name, { type });
}

describe("isSupported", () => {
  it("accepts pdf / docx / txt / md, rejects others", () => {
    for (const ext of ACCEPTED_EXTENSIONS) {
      expect(isSupported(file("x", `doc${ext}`))).toBe(true);
    }
    expect(isSupported(file("x", "IMAGE.PDF"))).toBe(true); // case-insensitive
    expect(isSupported(file("x", "scan.png"))).toBe(false);
    expect(isSupported(file("x", "data.csv"))).toBe(false);
    expect(isSupported(file("x", "noextension"))).toBe(false);
  });
});

describe("extractDocumentText — text formats", () => {
  it("reads .txt and .md directly", async () => {
    expect(await extractDocumentText(file("Build a portal.", "brief.txt"))).toBe("Build a portal.");
    expect(await extractDocumentText(file("# RFP\n\nScope here", "rfp.md", "text/markdown"))).toBe(
      "# RFP\n\nScope here",
    );
  });

  it("normalizes whitespace (CRLF, trailing spaces, blank runs, gappy spaces)", async () => {
    const raw = "Line one  \r\n\r\n\r\n\r\nLine    two   \n";
    expect(await extractDocumentText(file(raw, "messy.txt"))).toBe("Line one\n\nLine two");
  });
});

describe("extractDocumentText — errors", () => {
  it("rejects an unsupported extension", async () => {
    await expect(extractDocumentText(file("x", "photo.png"))).rejects.toBeInstanceOf(
      DocumentExtractError,
    );
  });

  it("rejects an oversize file before parsing", async () => {
    const big = file(new Uint8Array(MAX_FILE_BYTES + 1), "huge.txt");
    await expect(extractDocumentText(big)).rejects.toBeInstanceOf(DocumentExtractError);
  });

  it("rejects a document with no readable text", async () => {
    await expect(extractDocumentText(file("   \n\n  ", "blank.txt"))).rejects.toThrow(/no readable text/i);
  });
});

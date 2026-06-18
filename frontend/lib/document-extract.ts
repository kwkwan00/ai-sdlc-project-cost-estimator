/** Client-side text extraction from an uploaded project-description document.
 *
 *  Plain text (.txt/.md) is read directly; PDF (pdf.js) and Word (.docx, mammoth) are parsed
 *  via **dynamically-imported** libraries so those heavy parsers are split into their own
 *  chunks and loaded only when a matching file is actually uploaded — the Stage 1 page stays
 *  light. The extracted text is normalized and returned; the caller drops it into the existing
 *  description box (so the rest of the flow — prefill, raw_input — is unchanged). */

export const ACCEPTED_EXTENSIONS = [".txt", ".md", ".markdown", ".pdf", ".docx"] as const;

/** `accept` attribute for the file input — extensions + MIME types. */
export const ACCEPT_ATTR =
  ".txt,.md,.markdown,.pdf,.docx," +
  "text/plain,text/markdown,application/pdf," +
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

export const MAX_FILE_BYTES = 10 * 1024 * 1024; // 10 MB — generous for an RFP/SOW

/** Thrown for user-actionable problems (unsupported type, oversize, unreadable / scanned). */
export class DocumentExtractError extends Error {}

function extensionOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

export function isSupported(file: File): boolean {
  return (ACCEPTED_EXTENSIONS as readonly string[]).includes(extensionOf(file.name));
}

function normalizeWhitespace(s: string): string {
  return s
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t]+\n/g, "\n") // strip trailing spaces
    .replace(/[ \t]{2,}/g, " ") // collapse runs of spaces (PDF extraction is gappy)
    .replace(/\n{3,}/g, "\n\n") // collapse blank-line runs
    .trim();
}

/** Extract plain text from a supported document. Throws {@link DocumentExtractError} on an
 *  unsupported type, an oversize file, or a parse failure / empty result. */
export async function extractDocumentText(file: File): Promise<string> {
  if (file.size > MAX_FILE_BYTES) {
    throw new DocumentExtractError(
      `That file is ${(file.size / 1024 / 1024).toFixed(1)} MB — the limit is ${MAX_FILE_BYTES / 1024 / 1024} MB.`,
    );
  }
  const ext = extensionOf(file.name);
  let text: string;
  switch (ext) {
    case ".txt":
    case ".md":
    case ".markdown":
      text = await file.text();
      break;
    case ".pdf":
      text = await extractPdf(file);
      break;
    case ".docx":
      text = await extractDocx(file);
      break;
    default:
      throw new DocumentExtractError(
        `Unsupported file type "${ext || file.name}". Upload a PDF, Word (.docx), or text (.txt/.md) file.`,
      );
  }
  const cleaned = normalizeWhitespace(text);
  if (!cleaned) {
    throw new DocumentExtractError(
      "No readable text was found — the document may be scanned or image-only. Paste the description instead.",
    );
  }
  return cleaned;
}

async function extractPdf(file: File): Promise<string> {
  const pdfjs = await import("pdfjs-dist");
  // Bundle the worker as its own asset (offline-friendly; no CDN dependency).
  pdfjs.GlobalWorkerOptions.workerSrc = new URL(
    "pdfjs-dist/build/pdf.worker.min.mjs",
    import.meta.url,
  ).toString();
  const task = pdfjs.getDocument({ data: await file.arrayBuffer() });
  let doc;
  try {
    doc = await task.promise;
  } catch (e) {
    throw new DocumentExtractError(`Couldn't read the PDF (${(e as Error).message}).`);
  }
  try {
    const pages: string[] = [];
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const content = await page.getTextContent();
      pages.push(content.items.map((it) => ("str" in it ? it.str : "")).join(" "));
    }
    return pages.join("\n\n");
  } finally {
    await task.destroy();
  }
}

async function extractDocx(file: File): Promise<string> {
  const mammoth = await import("mammoth");
  try {
    const { value } = await mammoth.extractRawText({ arrayBuffer: await file.arrayBuffer() });
    return value;
  } catch (e) {
    throw new DocumentExtractError(`Couldn't read the Word document (${(e as Error).message}).`);
  }
}

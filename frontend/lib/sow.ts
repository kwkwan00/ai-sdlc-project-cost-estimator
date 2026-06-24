/** Client-side helpers for the SOW export feature. The pure ones are unit-tested. */

import type { SowDocument, SowSectionContent } from "./types";

/** Download filename for the .docx, mirroring the backend's sanitization. */
export function sowFilename(projectName: string): string {
  const base =
    (projectName || "estimate")
      .replace(/[^A-Za-z0-9 _-]/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 80)
      .trim() || "estimate";
  return `${base} - SOW.docx`;
}

/** Immutably replace one section (by id) in a document — for the editable preview. */
export function replaceSection(
  doc: SowDocument,
  id: string,
  patch: Partial<SowSectionContent>
): SowDocument {
  return {
    ...doc,
    sections: doc.sections.map((s) => (s.id === id ? { ...s, ...patch } : s)),
  };
}

/** Trigger a browser download of a Blob (DOM side-effect; not unit-tested). */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

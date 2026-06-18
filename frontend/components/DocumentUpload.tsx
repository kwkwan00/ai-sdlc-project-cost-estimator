"use client";

import { useRef, useState } from "react";

import {
  ACCEPT_ATTR,
  DocumentExtractError,
  extractDocumentText,
  isSupported,
} from "@/lib/document-extract";

interface Props {
  /** Called with the extracted text + original file name once a document is read. */
  onExtracted: (text: string, fileName: string) => void;
}

/** Stage 1 helper: upload (or drop) a PDF / Word / text file and extract its text into the
 *  project-description box. Parsing runs entirely client-side (see `lib/document-extract`). */
export function DocumentUpload({ onExtracted }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [loaded, setLoaded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleFile(f: File) {
    setError(null);
    setLoaded(null);
    if (!isSupported(f)) {
      setError("Unsupported file — upload a PDF, Word (.docx), or text (.txt/.md) file.");
      return;
    }
    setBusy(true);
    try {
      const text = await extractDocumentText(f);
      setLoaded(f.name);
      onExtracted(text, f.name);
    } catch (e) {
      setError(
        e instanceof DocumentExtractError
          ? e.message
          : `Couldn't read the file (${(e as Error).message}).`,
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        const f = e.dataTransfer.files?.[0];
        if (f) void handleFile(f);
      }}
      className={`rounded-md border border-dashed px-3 py-2 text-xs transition ${
        dragging ? "border-brand-400 bg-brand-50" : "border-slate-300 bg-slate-50"
      }`}
    >
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT_ATTR}
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void handleFile(f);
          e.target.value = ""; // allow re-selecting the same file
        }}
      />
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={busy}
          className="btn-secondary px-2 py-1 text-xs disabled:opacity-60 disabled:cursor-progress"
        >
          {busy ? "Reading…" : "Upload a document"}
        </button>
        <span className="muted">or drop a PDF, Word, or text file to fill the description.</span>
        {loaded && !error && (
          <span className="text-emerald-600">Loaded &ldquo;{loaded}&rdquo; — review below.</span>
        )}
      </div>
      {error && <p className="mt-1 text-rose-600">{error}</p>}
    </div>
  );
}

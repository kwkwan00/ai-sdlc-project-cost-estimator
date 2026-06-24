"use client";

import { useEffect, useState } from "react";

import { downloadSowDocx, generateSow } from "@/lib/api-client";
import { downloadBlob, replaceSection, sowFilename } from "@/lib/sow";
import type {
  LlmUsage,
  SowDocument,
  SowScenario,
  SowSectionContent,
} from "@/lib/types";

import { Modal } from "./Modal";

interface Props {
  open: boolean;
  onClose: () => void;
  estimateId: string;
  projectName: string;
  /** Which cost scenario drives the fee table — mirrors the review page toggle. */
  scenario: SowScenario;
}

/** Generate → edit → download a Statement of Work. One LLM call on open;
 *  the user edits prose/bullets in place, then downloads an editable .docx. */
export function SowExportModal({ open, onClose, estimateId, projectName, scenario }: Props) {
  const [doc, setDoc] = useState<SowDocument | null>(null);
  const [genScenario, setGenScenario] = useState<SowScenario | null>(null);
  const [usage, setUsage] = useState<LlmUsage | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Generate when opened, and regenerate when the scenario changes. Edits are preserved
  // across a close/reopen with the same scenario (we keep the doc unless scenario differs).
  useEffect(() => {
    if (!open) return;
    if (doc && genScenario === scenario) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    generateSow(estimateId, scenario)
      .then((res) => {
        if (cancelled) return;
        setDoc(res.document);
        setGenScenario(scenario);
        setUsage(res.llm_usage);
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [open, scenario, estimateId, doc, genScenario]);

  async function handleDownload() {
    if (!doc) return;
    setDownloading(true);
    setError(null);
    try {
      const blob = await downloadSowDocx(estimateId, doc);
      downloadBlob(blob, sowFilename(doc.project_name || projectName));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDownloading(false);
    }
  }

  function editSection(id: string, patch: Partial<SowSectionContent>) {
    setDoc((d) => (d ? replaceSection(d, id, patch) : d));
  }

  return (
    <Modal open={open} onClose={onClose} title="Export Statement of Work" widthClass="max-w-4xl">
      <p className="muted mb-3 text-sm">
        A formatted Statement of Work draft generated from this estimate. Edit the prose below, then
        download an editable Word document. Bracketed{" "}
        <code className="rounded bg-slate-100 px-1">[PLACEHOLDERS]</code> are filled in Word.
      </p>

      {loading && <p className="text-sm text-slate-500">Generating draft…</p>}

      {error && (
        <p className="mb-3 rounded-md bg-red-50 p-2 text-sm text-red-700" role="alert">
          {error}
        </p>
      )}

      {doc && !loading && (
        <>
          {doc.placeholders.length > 0 && (
            <div className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm">
              <p className="font-medium text-amber-800">Fill these in Word before sending:</p>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {doc.placeholders.map((p) => (
                  <code key={p} className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-900">
                    {p}
                  </code>
                ))}
              </div>
            </div>
          )}

          <div className="space-y-4">
            {doc.sections.map((s) => (
              <SectionEditor key={s.id} section={s} onChange={(patch) => editSection(s.id, patch)} />
            ))}
          </div>

          <div className="mt-5 flex items-center justify-between gap-4 border-t border-slate-200 pt-4">
            <span className="text-xs text-slate-400">
              {usage && usage.call_count > 0
                ? `Draft written by AI · $${usage.cost_usd.toFixed(4)}`
                : "Draft assembled from the template"}
            </span>
            <button
              type="button"
              onClick={handleDownload}
              disabled={downloading}
              className="btn-primary text-sm disabled:opacity-50"
            >
              {downloading ? "Preparing…" : "Download .docx"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

/** One section of the preview: editable textarea for prose/bullets, read-only for the rest. */
function SectionEditor({
  section,
  onChange,
}: {
  section: SowSectionContent;
  onChange: (patch: Partial<SowSectionContent>) => void;
}) {
  const label = section.heading || section.id.replace(/_/g, " ");

  return (
    <div>
      <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</p>

      {section.editable && section.kind === "paragraph" && (
        <textarea
          value={section.text}
          onChange={(e) => onChange({ text: e.target.value })}
          rows={Math.min(8, Math.max(2, Math.ceil(section.text.length / 90)))}
          className="w-full rounded-md border border-slate-300 p-2 text-sm"
        />
      )}

      {section.editable && section.kind === "bullets" && (
        <BulletEditor bullets={section.bullets} onChange={(bullets) => onChange({ bullets })} />
      )}

      {section.kind === "cover" && (
        <p className="rounded-md bg-slate-50 p-2 text-sm text-slate-600">{section.text}</p>
      )}

      {!section.editable && section.kind === "bullets" && (
        <ul className="list-disc space-y-1 pl-5 text-sm">
          {section.bullets.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      )}

      {section.kind === "table" && section.table && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr>
                {section.table.columns.map((c) => (
                  <th key={c} className="border border-slate-200 bg-slate-50 px-2 py-1 text-left font-semibold">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {section.table.rows.map((row, ri) => (
                <tr key={ri}>
                  {row.map((cell, ci) => (
                    <td key={ci} className="border border-slate-200 px-2 py-1">
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {section.kind === "signature_block" && (
        <div className="rounded-md bg-slate-50 p-2 text-sm text-slate-600">
          {section.text && <p className="mb-2">{section.text}</p>}
          <div className="flex flex-wrap gap-6">
            {section.signatories.map((sig) => (
              <div key={sig.party}>
                <p className="font-medium">{sig.party}</p>
                {sig.fields.map((f) => (
                  <p key={f} className="text-slate-400">
                    {f}: ____________
                  </p>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** Editable bulleted list: one bullet row per item, with add/remove. */
function BulletEditor({
  bullets,
  onChange,
}: {
  bullets: string[];
  onChange: (bullets: string[]) => void;
}) {
  return (
    <ul className="space-y-1">
      {bullets.map((b, i) => (
        <li key={i} className="flex items-start gap-2">
          <span className="mt-2 select-none text-slate-400">•</span>
          <input
            type="text"
            value={b}
            onChange={(e) => onChange(bullets.map((x, idx) => (idx === i ? e.target.value : x)))}
            className="flex-1 rounded-md border border-slate-300 p-1.5 text-sm"
          />
          <button
            type="button"
            onClick={() => onChange(bullets.filter((_, idx) => idx !== i))}
            aria-label="Remove item"
            className="mt-1 inline-flex h-6 w-6 items-center justify-center rounded text-slate-400 hover:bg-slate-100 hover:text-red-600"
          >
            ×
          </button>
        </li>
      ))}
      <li>
        <button
          type="button"
          onClick={() => onChange([...bullets, ""])}
          className="text-xs font-medium text-brand-600 hover:underline"
        >
          + Add item
        </button>
      </li>
    </ul>
  );
}

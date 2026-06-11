"use client";

import { useEffect } from "react";

import type { RosterPlanItem } from "@/lib/roster-agui";

interface Props {
  open: boolean;
  rationale: string;
  projectPlan: RosterPlanItem[];
  onClose: () => void;
}

/** Dismissible modal shown once the AG-UI roster agent proposes a team. Echoes
 *  the staffing rationale + high-level delivery plan so the user understands why
 *  the roster was staffed the way it was. */
export function RosterRationaleModal({
  open,
  rationale,
  projectPlan,
  onClose,
}: Props) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="roster-modal-title"
      onClick={onClose}
    >
      <div
        className="card max-w-lg w-full space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3">
          <h2
            id="roster-modal-title"
            className="text-lg font-bold text-slate-900"
          >
            Proposed team
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 shrink-0"
            aria-label="Dismiss"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.75}
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-5 w-5"
              aria-hidden="true"
            >
              <path d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {rationale && (
          <p className="text-sm text-slate-700 leading-snug">{rationale}</p>
        )}

        {projectPlan.length > 0 && (
          <div className="space-y-1">
            <p className="text-xs font-semibold uppercase tracking-wide muted">
              Delivery plan
            </p>
            <ul className="space-y-1">
              {projectPlan.map((p, i) => (
                <li key={`${p.workstream}-${i}`} className="text-sm text-slate-700">
                  <span className="font-medium">{p.workstream}</span>
                  {p.summary ? <span className="muted"> — {p.summary}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        )}

        <p className="text-[10px] muted">
          You can edit the roster below — these are just a starting point.
        </p>

        <div className="flex justify-end pt-1">
          <button type="button" onClick={onClose} className="btn-primary text-sm">
            Got it
          </button>
        </div>
      </div>
    </div>
  );
}

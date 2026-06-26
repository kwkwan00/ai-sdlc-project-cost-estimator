"use client";

import { PHASE_LABELS, type Phase } from "@/lib/types";

/** Canonical SDLC phase order — the single source for both the Quick-Estimate (Stage 1) and WBS
 *  describe wizards' scope pickers. Callers seed their `selected` state with this. */
export const ALL_PHASES = Object.keys(PHASE_LABELS) as Phase[];

interface Props {
  selected: Phase[];
  onChange: (next: Phase[]) => void;
  /** Override the explanatory sentence (the two wizards word it slightly differently). */
  description?: string;
}

/** Shared "Phases to estimate" checkbox grid. Toggling preserves canonical order and the caller
 *  owns the `selected` state + the omit-when-all request contract; this renders the grid + the
 *  "select at least one" hint so the Quick-Estimate and WBS wizards stay in lock-step. */
export function PhaseScopePicker({ selected, onChange, description }: Props) {
  const toggle = (phase: Phase) =>
    onChange(
      selected.includes(phase)
        ? selected.filter((p) => p !== phase)
        : ALL_PHASES.filter((p) => p === phase || selected.includes(p)), // keep canonical order
    );

  return (
    <div className="space-y-2">
      <div className="space-y-1">
        <span className="label">Phases to estimate</span>
        <p className="muted text-sm">
          {description ??
            "By default we estimate the full SDLC. Uncheck any phases to leave them out — cost, timeline, and team are rolled up from only the phases you keep."}
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {ALL_PHASES.map((phase) => (
          <label
            key={phase}
            className="flex cursor-pointer items-center gap-2 rounded-md border border-slate-200 px-3 py-2 text-sm hover:bg-slate-50"
          >
            <input
              type="checkbox"
              className="h-4 w-4"
              checked={selected.includes(phase)}
              onChange={() => toggle(phase)}
            />
            <span className="text-slate-800">{PHASE_LABELS[phase]}</span>
          </label>
        ))}
      </div>
      {selected.length === 0 && (
        <p className="text-sm text-rose-600">Select at least one phase to estimate.</p>
      )}
    </div>
  );
}

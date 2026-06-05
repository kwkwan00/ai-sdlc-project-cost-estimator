"use client";

import { useId } from "react";

interface Props {
  /** Tooltip body — kept short, one or two sentences. */
  text: string;
}

/** Small "?" trigger that reveals a tooltip on hover/focus.
 *
 * Pure-CSS show/hide via Tailwind's `group-hover` + `group-focus-within`, so
 * the tooltip is keyboard-accessible (the trigger is a focusable button) and
 * works without any JS state. Tooltip body is always rendered into the DOM so
 * `aria-describedby` can resolve it for assistive tech regardless of visibility.
 *
 * Positioning: anchored below the trigger, centered horizontally on the icon.
 * `max-w-xs` keeps long descriptions wrapping reasonably; if the trigger is
 * near the right edge of the viewport, the tooltip clips — that's an accepted
 * trade-off for a dep-free implementation.
 */
export function FieldHint({ text }: Props) {
  const id = useId();
  return (
    <span className="relative inline-flex group align-middle ml-1">
      <button
        type="button"
        aria-label="More information"
        aria-describedby={id}
        // The button never submits a form and isn't an "action"; it just shows
        // help text. Tab-stop keeps it keyboard-discoverable.
        className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 bg-slate-50 text-[10px] font-semibold text-slate-500 cursor-help hover:border-brand-600 hover:text-brand-600 focus:outline-none focus:border-brand-600 focus:text-brand-600 transition-colors"
        onClick={(e) => e.preventDefault()}
      >
        ?
      </button>
      <span
        id={id}
        role="tooltip"
        className="pointer-events-none absolute left-1/2 top-full mt-1 -translate-x-1/2 z-20 w-64 max-w-xs rounded-md bg-slate-900 px-3 py-2 text-xs leading-snug text-white shadow-lg opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}

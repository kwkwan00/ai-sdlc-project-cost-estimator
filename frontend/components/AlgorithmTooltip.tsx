"use client";

import { algorithmInfo } from "@/lib/algorithms";

/** A small "ⓘ" icon next to an estimation algorithm name that reveals a
 *  description on hover or keyboard focus. Renders nothing for unknown algorithms. */
export function AlgorithmTooltip({ algorithm }: { algorithm: string }) {
  const info = algorithmInfo(algorithm);
  if (!info) return null;

  return (
    <span className="group relative ml-1 inline-flex align-middle">
      <button
        type="button"
        aria-label={`About ${info.name}`}
        // Prevent a click from toggling a parent <details>/<summary>.
        onClick={(e) => e.preventDefault()}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px] font-semibold leading-none text-slate-500 hover:bg-slate-100 hover:text-slate-700 focus:outline-none focus:ring-2 focus:ring-brand-400"
      >
        i
      </button>
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-2 w-64 -translate-x-1/2 rounded-md bg-slate-900 px-3 py-2 text-left text-xs font-normal normal-case text-white opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100"
      >
        <span className="block font-semibold">{info.name}</span>
        <span className="mt-1 block text-slate-200">{info.description}</span>
      </span>
    </span>
  );
}

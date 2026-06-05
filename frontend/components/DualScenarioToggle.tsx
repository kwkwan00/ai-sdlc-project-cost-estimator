"use client";

import clsx from "clsx";

interface Props {
  value: "ai_assisted" | "manual_only";
  onChange: (v: "ai_assisted" | "manual_only") => void;
}

export function DualScenarioToggle({ value, onChange }: Props) {
  return (
    <div className="inline-flex rounded-md border border-slate-300 bg-white p-1 text-sm">
      <button
        type="button"
        onClick={() => onChange("ai_assisted")}
        className={clsx(
          "rounded px-3 py-1 transition",
          value === "ai_assisted"
            ? "bg-brand-600 text-white"
            : "text-slate-700 hover:bg-slate-100"
        )}
      >
        AI-assisted
      </button>
      <button
        type="button"
        onClick={() => onChange("manual_only")}
        className={clsx(
          "rounded px-3 py-1 transition",
          value === "manual_only"
            ? "bg-brand-600 text-white"
            : "text-slate-700 hover:bg-slate-100"
        )}
      >
        Manual-only
      </button>
    </div>
  );
}

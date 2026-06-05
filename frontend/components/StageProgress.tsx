import clsx from "clsx";

const STAGES = [
  { n: 1, label: "Describe" },
  { n: 2, label: "Context" },
  { n: 3, label: "Maturity" },
  { n: 4, label: "Questions" },
  { n: 5, label: "Review" },
];

export function StageProgress({ current }: { current: 1 | 2 | 3 | 4 | 5 }) {
  return (
    <ol className="flex items-center gap-2 text-xs">
      {STAGES.map((s) => (
        <li key={s.n} className="flex items-center gap-2">
          <span
            className={clsx(
              "h-6 w-6 rounded-full flex items-center justify-center font-semibold",
              s.n < current && "bg-brand-600 text-white",
              s.n === current && "bg-brand-600 text-white ring-2 ring-brand-500/40",
              s.n > current && "bg-slate-200 text-slate-500"
            )}
          >
            {s.n}
          </span>
          <span
            className={clsx(
              s.n === current ? "font-medium text-slate-900" : "text-slate-500"
            )}
          >
            {s.label}
          </span>
          {s.n < STAGES.length && <span className="text-slate-300">›</span>}
        </li>
      ))}
    </ol>
  );
}

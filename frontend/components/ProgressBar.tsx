import clsx from "clsx";

/** A determinate progress bar (0–100) with an optional phase label and percentage readout. The
 *  fill animates via a CSS width transition so stepwise `trickle` updates glide rather than jump. */
export function ProgressBar({
  value,
  label,
  className,
  showPercent = true,
}: {
  value: number;
  label?: string;
  className?: string;
  /** Show the numeric percentage. Off for time-based/indeterminate bars where the % would be
   *  misleading (e.g. the WBS draft, which narrates real status messages instead). */
  showPercent?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className={clsx("space-y-1.5", className)}>
      {label && (
        <div className="flex items-center justify-between gap-3 text-xs">
          <span className="min-w-0 truncate text-slate-600">{label}</span>
          {showPercent && (
            <span className="muted tabular-nums shrink-0">{Math.round(pct)}%</span>
          )}
        </div>
      )}
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-slate-200"
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label || "Progress"}
      >
        <div
          className="h-full rounded-full bg-brand-600 transition-[width] duration-300 ease-out"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

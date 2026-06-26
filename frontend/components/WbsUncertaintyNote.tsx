/** "Uncertainty & caveats" explainer for the Risk & uncertainty tab of a WBS (bottom-up) estimate.
 *
 *  A WBS roll-up produces a real Monte-Carlo confidence band (from each task's 3-point estimate) and
 *  applies a contingency reserve + a complexity realism factor — but it emits NO discrete risk
 *  register and carries a fixed baseline confidence. This read-only panel makes that explicit so an
 *  empty risk list isn't misread as "no risk", and states the method caveats that the band alone
 *  doesn't convey. */
export function WbsUncertaintyNote({
  contingencyPct,
  confidence,
}: {
  /** Contingency reserve % actually applied to this estimate (DualScenarioEstimate.contingency_pct). */
  contingencyPct: number;
  /** Baseline confidence 0–1 (fixed for WBS). */
  confidence: number;
}) {
  const cont = Math.round(contingencyPct);
  const conf = Math.round(confidence * 100);

  return (
    <section className="card space-y-3">
      <div>
        <h2 className="section-title">Uncertainty &amp; caveats</h2>
        <p className="text-xs muted">
          How to read this bottom-up estimate&apos;s uncertainty — and what it does and doesn&apos;t
          account for.
        </p>
      </div>

      <div className="space-y-2 text-sm text-slate-700">
        <p>
          <span className="font-medium">The confidence band is computed, not assumed.</span> It&apos;s
          a Monte-Carlo roll-up of every task&apos;s three-point estimate (optimistic / most-likely /
          pessimistic), so the P10–P90 spread reflects how those per-task ranges actually combine —
          not a fixed ± margin.
        </p>

        <div>
          <p className="font-medium">Buffers already applied to this estimate:</p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            <li>
              {cont > 0 ? (
                <>
                  <span className="font-medium">{cont}% contingency reserve</span> added to cost and
                  timeline — a deliberate management buffer, separate from the band above.
                </>
              ) : (
                <>
                  <span className="font-medium">No contingency reserve</span> was applied (set to
                  0%) — the band above is the only buffer.
                </>
              )}
            </li>
            <li>
              <span className="font-medium">A complexity realism factor</span> scaled the raw task
              hours up to correct the systematic optimism of bottom-up task lists.
            </li>
          </ul>
        </div>

        <p>
          <span className="font-medium">Confidence is a fixed {conf}% baseline.</span> Unlike the
          parametric estimate, a bottom-up WBS produces no algorithmic confidence signal — read it as
          &ldquo;moderate,&rdquo; not a precise probability.
        </p>
      </div>

      <div className="rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
        <p className="mb-1 font-medium text-slate-700">Keep in mind</p>
        <ul className="list-disc space-y-1 pl-4">
          <li>
            Bottom-up task lists tend to <span className="font-medium">miss work</span> — the band +
            contingency correct for it, but residual risk remains.
          </li>
          <li>
            The breakdown was <span className="font-medium">seeded by a single AI draft</span>, then
            edited — it&apos;s only as complete as the decomposition.
          </li>
          <li>
            The band is only as honest as the per-task three-point spreads — narrow or overconfident
            inputs understate the true range.
          </li>
          <li>
            The timeline is <span className="font-medium">presentational</span> (scaled to the
            reported duration), not a critical-path schedule; dependencies are captured but
            don&apos;t yet drive the duration.
          </li>
        </ul>
      </div>

      <p className="text-[11px] muted">
        Bottom-up WBS estimates don&apos;t generate a discrete risk register — the uncertainty lives
        in the band and buffers described here, not a per-risk list.
      </p>
    </section>
  );
}

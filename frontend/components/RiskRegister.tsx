"use client";

import { formatHours, formatPct } from "@/lib/format";
import { collectPhaseRisks } from "@/lib/risk";
import { PHASE_LABELS, type PhaseEstimate } from "@/lib/types";

interface Props {
  phases: PhaseEstimate[];
}

/** Cross-phase risk register: every phase's `risks` (probability × impact range),
 *  flattened and sorted by expected impact (`likelihood × midpoint(low, high)`),
 *  highest first. The expected-impact ranking lives in `lib/risk.ts`; this is the
 *  presentation. Independent of the AI/manual toggle — risks are scenario-agnostic. */
export function RiskRegister({ phases }: Props) {
  const risks = collectPhaseRisks(phases);

  if (risks.length === 0) {
    return <p className="text-sm muted">No risks flagged across the phases.</p>;
  }

  return (
    <table className="min-w-full text-sm">
      <thead>
        <tr className="text-left text-xs uppercase muted">
          <th className="py-2">Risk</th>
          <th className="py-2">Phase</th>
          <th className="py-2">Likelihood</th>
          <th className="py-2">Impact (hrs)</th>
          <th className="py-2">Expected</th>
        </tr>
      </thead>
      <tbody>
        {risks.map(({ phase, risk, expectedHours }, i) => (
          <tr key={`${phase}-${i}`} className="border-t border-slate-100 align-top">
            <td className="py-2 pr-3">{risk.description}</td>
            <td className="py-2 pr-3">
              <span className="whitespace-nowrap text-xs text-slate-500">
                {PHASE_LABELS[phase]}
              </span>
            </td>
            <td className="py-2 pr-3 whitespace-nowrap">{formatPct(risk.likelihood)}</td>
            <td className="py-2 pr-3 whitespace-nowrap text-slate-500">
              {formatHours(risk.impact_hours_low)} – {formatHours(risk.impact_hours_high)}
            </td>
            <td className="py-2 whitespace-nowrap font-semibold">
              {formatHours(expectedHours)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

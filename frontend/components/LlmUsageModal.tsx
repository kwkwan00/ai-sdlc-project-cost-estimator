"use client";

import { Modal } from "@/components/Modal";
import { formatTokens, formatUSDPrecise } from "@/lib/format";
import type { LlmUsage } from "@/lib/types";

/** Modal showing the Anthropic token cost of an LLM step — the API cost, call count, token totals,
 *  and a per-model breakdown. Reused by the estimate review page (cost to produce the estimate) and
 *  the WBS editor (cost to draft the tree). */
export function LlmUsageModal({
  open,
  onClose,
  usage,
  title,
  subtitle,
}: {
  open: boolean;
  onClose: () => void;
  usage: LlmUsage;
  title: string;
  subtitle?: string;
}) {
  return (
    <Modal open={open} onClose={onClose} title={title}>
      {subtitle && <p className="text-xs muted mb-3">{subtitle}</p>}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div>
          <p className="text-xs muted">API cost</p>
          <p className="text-2xl font-semibold mt-1">{formatUSDPrecise(usage.cost_usd)}</p>
        </div>
        <div>
          <p className="text-xs muted">LLM calls</p>
          <p className="text-2xl font-semibold mt-1">{usage.call_count}</p>
        </div>
        <div>
          <p className="text-xs muted">Input tokens</p>
          <p className="text-2xl font-semibold mt-1">{formatTokens(usage.input_tokens)}</p>
        </div>
        <div>
          <p className="text-xs muted">Output tokens</p>
          <p className="text-2xl font-semibold mt-1">{formatTokens(usage.output_tokens)}</p>
        </div>
      </div>
      {usage.by_model.length > 0 && (
        <table className="mt-4 min-w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase muted">
              <th className="py-2">Model</th>
              <th className="py-2">Calls</th>
              <th className="py-2">Input</th>
              <th className="py-2">Output</th>
              <th className="py-2">Cost</th>
            </tr>
          </thead>
          <tbody>
            {usage.by_model.map((m) => (
              <tr key={m.model} className="border-t border-slate-100">
                <td className="py-2 font-medium">{m.model}</td>
                <td className="py-2">{m.calls}</td>
                <td className="py-2">{formatTokens(m.input_tokens)}</td>
                <td className="py-2">{formatTokens(m.output_tokens)}</td>
                <td className="py-2 font-semibold">{formatUSDPrecise(m.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Modal>
  );
}

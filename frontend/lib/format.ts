export function formatHours(n: number): string {
  if (n >= 10_000) return `${Math.round(n).toLocaleString()} h`;
  return `${Math.round(n)} h`;
}

export function formatUSD(n: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

export function formatPct(n: number): string {
  return `${Math.round(n * 100)}%`;
}

/** Cents precision for small amounts (e.g. LLM cost), whole dollars for large. */
export function formatUSDPrecise(n: number): string {
  const digits = Math.abs(n) < 100 ? 2 : 0;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(n);
}

/** Compact token counts: 842 → "842", 58239 → "58.2k", 1.2M → "1.2M". */
export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.?0+$/, "")}k`;
  return `${(n / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
}

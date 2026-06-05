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

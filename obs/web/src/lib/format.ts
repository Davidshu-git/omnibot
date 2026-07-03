export function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function fmtCost(c: number): string {
  if (c < 0.001) return "< ¥0.001";
  if (c < 1) return `¥${c.toFixed(3)}`;
  return `¥${c.toFixed(2)}`;
}

export function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

/** 带正负号的百分比。 */
export function fmtPct(n: number | undefined | null): string {
  if (n === undefined || n === null) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
}

/** 盈亏正红负绿（涨红跌绿，A 股语义）。 */
export function pnlColor(n: number | undefined | null): string {
  if (n === undefined || n === null || n === 0) return "var(--text)";
  return n > 0 ? "var(--red)" : "var(--green)";
}

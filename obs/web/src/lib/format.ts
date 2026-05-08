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

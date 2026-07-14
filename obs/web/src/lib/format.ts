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

const _CURRENCY_SYMBOL: Record<string, string> = { USD: "$", HKD: "HK$", CNY: "¥" };

/** 大额金额（市值/成交量）带币种符号 + 万亿/亿/万 中文单位缩写。
 * currency 未知时以「代码+空格」前缀。null/undefined 返回「—」。 */
export function fmtBigMoney(n: number | undefined | null, currency?: string | null): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return "—";
  const prefix = currency ? (_CURRENCY_SYMBOL[currency] ?? `${currency} `) : "";
  const abs = Math.abs(n);
  if (abs >= 1e12) return `${prefix}${(n / 1e12).toFixed(2)}万亿`;
  if (abs >= 1e8) return `${prefix}${(n / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${prefix}${(n / 1e4).toFixed(2)}万`;
  return `${prefix}${n.toFixed(0)}`;
}

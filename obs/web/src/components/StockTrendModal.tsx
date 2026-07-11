import { useEffect, useState } from "react";
import {
  api,
  type StockTrend,
  type StockTrendMaInfo,
  type StockTrendPoint,
  type StockTrendTrade,
} from "@/lib/api";
import { fmtPct, pnlColor } from "@/lib/format";

/** 个股趋势分析弹窗：点击持仓行 / 筛股结果行触发，独立 Modal（Esc / 点击遮罩 / 关闭按钮均可关闭）。
 * 共享组件——investment portfolio 总控台与选股筛股页均复用同一份实现。 */
type TrendLineKey = "close" | "ma20" | "ma60" | "ma250";
const ALL_LINES_VISIBLE: Record<TrendLineKey, boolean> = { close: true, ma20: true, ma60: true, ma250: true };

// 显示窗口档位——须与估值引擎 TREND_WINDOWS 白名单一致（后端多取一年做均线预热，
// 短窗口下 MA250 依然有效；切窗口只裁剪可见范围，均线方向/分位结论不变）。
const TREND_PERIODS = [
  { value: "6mo", label: "6月" },
  { value: "1y", label: "1年" },
  { value: "2y", label: "2年" },
  { value: "5y", label: "5年" },
  { value: "max", label: "全部" },
] as const;
type TrendPeriod = (typeof TREND_PERIODS)[number]["value"];
const DEFAULT_PERIOD: TrendPeriod = "2y";

export function StockTrendModal({ ticker, onClose }: { ticker: string | null; onClose: () => void }) {
  const [data, setData] = useState<StockTrend | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [visible, setVisible] = useState<Record<TrendLineKey, boolean>>(ALL_LINES_VISIBLE);
  const [period, setPeriod] = useState<TrendPeriod>(DEFAULT_PERIOD);
  const toggleLine = (key: TrendLineKey) => setVisible((v) => ({ ...v, [key]: !v[key] }));

  useEffect(() => {
    if (!ticker) return;
    setLoading(true);
    setErr("");
    setData(null);
    setVisible(ALL_LINES_VISIBLE); // 每次换标的重置线条可见性，避免带着上一个 ticker 的隐藏状态
    api.stockTrend(ticker, period)
      .then((d) => {
        if (d.status !== "ok") {
          setErr(d.detail || "查询失败");
        } else {
          setData(d);
        }
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [ticker, period]);

  useEffect(() => {
    if (!ticker) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ticker, onClose]);

  if (!ticker) return null;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
        display: "flex", alignItems: "center", justifyContent: "center",
        zIndex: 100, padding: "1rem",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="card"
        style={{ padding: "1.25rem", maxWidth: 720, width: "100%", maxHeight: "85vh", overflowY: "auto" }}
      >
        <div style={{ display: "flex", alignItems: "center", marginBottom: "1rem" }}>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--text)", margin: 0, fontFamily: "var(--font-mono)" }}>
            {ticker} 趋势分析
          </h2>
          <button onClick={onClose} className="tag-btn" style={{ marginLeft: "auto", fontSize: 12 }}>✕ 关闭</button>
        </div>

        {/* 显示窗口切换：切换即重新拉取（bot 侧按 ticker+period 各缓存 5 分钟） */}
        <div style={{ display: "flex", gap: 6, marginBottom: "0.75rem" }}>
          {TREND_PERIODS.map((p) => (
            <button
              key={p.value}
              onClick={() => setPeriod(p.value)}
              disabled={loading}
              className={`tag-btn${period === p.value ? " active" : ""}`}
              style={{ fontSize: 11 }}
            >
              {p.label}
            </button>
          ))}
        </div>

        {loading && <p style={{ color: "var(--text-dim)", fontSize: 13 }}>加载中…</p>}
        {err && <p style={{ color: "var(--red)", fontSize: 13 }}>{err}</p>}

        {data && data.status === "ok" && data.series && (
          <>
            <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 10 }}>
              <span style={{ fontSize: 22, fontWeight: 800, color: "var(--text)", fontVariantNumeric: "tabular-nums" }}>
                {data.latest_price?.toLocaleString("en-US", { maximumFractionDigits: 2 })}
              </span>
              <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{data.latest_date}</span>
            </div>

            <StockTrendChart series={data.series} visible={visible} trades={data.trades ?? []} />

            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 10, marginBottom: 12 }}>
              <TrendLegendRow label="现价" color="var(--text)" active={visible.close} onToggle={() => toggleLine("close")} />
              <MaInfoRow label="MA20" color="var(--chart-ma20)" info={data.ma20} active={visible.ma20} onToggle={() => toggleLine("ma20")} />
              <MaInfoRow label="MA60（中期）" color="var(--chart-ma60)" info={data.ma60} active={visible.ma60} onToggle={() => toggleLine("ma60")} />
              <MaInfoRow label="MA250（年线 / 大势）" color="var(--chart-ma250)" info={data.ma250} active={visible.ma250} onToggle={() => toggleLine("ma250")} />
            </div>
            <p style={{ color: "var(--text-dim)", fontSize: 11, margin: "-4px 0 10px" }}>点击图例可显示/隐藏对应线条</p>

            {data.regime_note && (
              <div className="card" style={{ padding: "0.6rem 0.85rem", marginBottom: 10, borderColor: "var(--border-hi)" }}>
                <span style={{ color: "var(--text-muted)", fontSize: 12 }}>{data.regime_note}</span>
              </div>
            )}

            {data.trades && data.trades.length > 0 && (
              <TradeList trades={data.trades} />
            )}

            <p style={{ color: "var(--text-dim)", fontSize: 11, margin: 0 }}>
              以上为价格相对均线位置的描述性指标（趋势方向 + 历史分位），不构成买卖建议，据此操作风险自负。
              {data.trades && data.trades.length > 0 && "图上标记点按交易日实际收盘价定位，与你记录时手写的成交价可能有出入，以下方明细为准。"}
            </p>
          </>
        )}
      </div>
    </div>
  );
}

/** 趋势图例行（无数值，仅标注线条含义，如"现价"）；点击切换该线条在图上的显示/隐藏。 */
function TrendLegendRow({ label, color, active, onToggle }: {
  label: string; color: string; active: boolean; onToggle: () => void;
}) {
  return (
    <div
      onClick={onToggle}
      style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, cursor: "pointer", opacity: active ? 1 : 0.4 }}
    >
      <span style={{ width: 10, height: 2, background: color, flexShrink: 0 }} />
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
    </div>
  );
}

/** 单条均线的图例 + 方向 + 偏离度 + 历史分位；数据不足时优雅降级为提示文案。
 * 点击切换该均线在图上的显示/隐藏（数据不足时图上本就无此线，不响应点击）。 */
function MaInfoRow({ label, color, info, active, onToggle }: {
  label: string; color: string; info?: StockTrendMaInfo; active: boolean; onToggle: () => void;
}) {
  if (!info || !info.available) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
        <span style={{ width: 10, height: 2, background: color, flexShrink: 0 }} />
        <span style={{ color: "var(--text-dim)" }}>{label}：历史数据不足</span>
      </div>
    );
  }
  // 涨红跌绿（A 股语义，与本页 pnlColor 一致）：均线向上视同"涨"。
  const dirColor = info.direction === "向上" ? "var(--red)" : info.direction === "向下" ? "var(--green)" : "var(--text-dim)";
  return (
    <div
      onClick={onToggle}
      style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, flexWrap: "wrap", cursor: "pointer", opacity: active ? 1 : 0.4 }}
    >
      <span style={{ width: 10, height: 2, background: color, flexShrink: 0 }} />
      <span style={{ color: "var(--text-muted)", minWidth: 110 }}>{label}</span>
      <span style={{ color: "var(--text)", fontVariantNumeric: "tabular-nums" }}>
        {info.value?.toLocaleString("en-US", { maximumFractionDigits: 2 })}
      </span>
      <span style={{ color: dirColor, fontWeight: 600 }}>{info.direction}</span>
      <span style={{ color: pnlColor(info.deviation_pct) }}>偏离 {fmtPct(info.deviation_pct)}</span>
      {info.deviation_percentile !== null && info.deviation_percentile !== undefined && (
        <span style={{ color: "var(--text-dim)" }}>历史分位 {info.deviation_percentile.toFixed(0)}%</span>
      )}
    </div>
  );
}

/** 历史买卖点明细（图上圆点对应的原始交易流水，含用户当时手写的自由文本）。 */
function TradeList({ trades }: { trades: StockTrendTrade[] }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <span style={{
        display: "block", color: "var(--text-muted)", fontSize: 11, fontWeight: 600,
        letterSpacing: "0.04em", textTransform: "uppercase", marginBottom: "0.5rem",
      }}>买卖记录（{trades.length}）</span>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {trades.slice().reverse().map((t, i) => (
          <div key={i} style={{ display: "flex", gap: 8, fontSize: 12, alignItems: "baseline" }}>
            <span style={{ color: t.side === "buy" ? "var(--red)" : "var(--green)", fontWeight: 600, minWidth: 32, flexShrink: 0 }}>
              {t.side === "buy" ? "买入" : "卖出"}
            </span>
            <span style={{ color: "var(--text-dim)", fontFamily: "var(--font-mono)", flexShrink: 0 }}>{t.date}</span>
            <span style={{ color: "var(--text-muted)" }}>{t.details}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

const TREND_LINE_KEYS: TrendLineKey[] = ["close", "ma20", "ma60", "ma250"];

/** 价格 + MA20/60/250 多线走势图 + 历史买卖点（内联 SVG，无第三方图表库）。
 * MA 序列前段可能为 null（历史不足以计算），按连续非空段分别画 polyline，不整段连线。
 * 隐藏的线条既不参与 y 轴范围计算也不画出——隐藏 MA250 之类的长线后图会自动缩放聚焦到剩余线条。
 * 买卖点始终参与 y 轴范围计算（不随 visible.close 隐藏而被裁切出可视区）。 */
function StockTrendChart({ series, visible, trades }: {
  series: StockTrendPoint[]; visible: Record<TrendLineKey, boolean>; trades: StockTrendTrade[];
}) {
  const W = 640, H = 220, padT = 10, padB = 8;
  if (series.length < 2) return null;

  const visibleKeys = TREND_LINE_KEYS.filter((k) => visible[k]);
  const allVals = series.flatMap((p) => visibleKeys.map((k) => p[k]))
    .concat(trades.map((t) => t.price))
    .filter((v): v is number => v !== null && v !== undefined);
  if (allVals.length === 0) return null; // 全部线条被隐藏，不渲染空图

  const min = Math.min(...allVals);
  const max = Math.max(...allVals);
  const range = max - min || 1;
  const x = (i: number) => (i / (series.length - 1)) * W;
  const y = (v: number) => padT + (1 - (v - min) / range) * (H - padT - padB);

  const buildSegments = (key: TrendLineKey): string[] => {
    const segments: string[] = [];
    let current: string[] = [];
    series.forEach((p, i) => {
      const v = p[key];
      if (v === null || v === undefined) {
        if (current.length > 1) segments.push(current.join(" "));
        current = [];
        return;
      }
      current.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    });
    if (current.length > 1) segments.push(current.join(" "));
    return segments;
  };

  // 买卖点按 date 字符串匹配到 series 里的下标（后端已把交易日对齐到最近的实际交易日）。
  const dateIndex = new Map(series.map((p, i) => [p.date, i]));

  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ display: "block", width: "100%", height: H }}>
      {visible.ma250 && buildSegments("ma250").map((pts, i) => (
        <polyline key={`ma250-${i}`} points={pts} fill="none" stroke="var(--chart-ma250)" strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
      ))}
      {visible.ma60 && buildSegments("ma60").map((pts, i) => (
        <polyline key={`ma60-${i}`} points={pts} fill="none" stroke="var(--chart-ma60)" strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
      ))}
      {visible.ma20 && buildSegments("ma20").map((pts, i) => (
        <polyline key={`ma20-${i}`} points={pts} fill="none" stroke="var(--chart-ma20)" strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
      ))}
      {visible.close && buildSegments("close").map((pts, i) => (
        <polyline key={`close-${i}`} points={pts} fill="none" stroke="var(--text)" strokeWidth={1.6} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      ))}
      {trades.map((t, i) => {
        const idx = dateIndex.get(t.date);
        if (idx === undefined) return null;
        // 涨红跌绿（A 股语义，与本页 pnlColor 一致）：买入沿用"涨/主动"红，卖出沿用"跌/离场"绿。
        const color = t.side === "buy" ? "var(--red)" : "var(--green)";
        return (
          <circle
            key={`trade-${i}`}
            cx={x(idx)}
            cy={y(t.price)}
            r={4}
            fill={color}
            stroke="var(--card)"
            strokeWidth={1}
            vectorEffect="non-scaling-stroke"
          >
            <title>{`${t.side === "buy" ? "买入" : "卖出"} ${t.date} @ ${t.price}`}</title>
          </circle>
        );
      })}
    </svg>
  );
}

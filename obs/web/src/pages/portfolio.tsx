import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  type PortfolioSnapshot,
  type PortfolioHolding,
  type PortfolioCashHolding,
  type FxTrend,
} from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useIsMobile } from "@/lib/useIsMobile";

/** ¥ 金额格式（千分位，两位小数）。 */
function fmtCny(n: number | undefined | null): string {
  if (n === undefined || n === null) return "—";
  return `¥${n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** 带正负号的百分比。 */
function fmtPct(n: number | undefined | null): string {
  if (n === undefined || n === null) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
}

/** 盈亏正绿负红（涨绿跌红，A 股语义）。 */
function pnlColor(n: number | undefined | null): string {
  if (n === undefined || n === null || n === 0) return "var(--text)";
  return n > 0 ? "var(--red)" : "var(--green)";
}

const CURRENCY_LABEL: Record<string, string> = { USD: "美元", HKD: "港币", CNY: "人民币" };
const ALLOC_PALETTE = ["var(--cat-1)", "var(--cat-2)", "var(--cat-3)", "var(--blue)", "var(--amber)"];

// Ghostfolio 范式的时间窗骨架；v1 仅「累计」可用，其余窗待快照积累（二档）。
const TIME_WINDOWS = ["今日", "WTD", "MTD", "YTD", "1Y", "累计"] as const;

export default function PortfolioPage() {
  const isMobile = useIsMobile();
  const [snap, setSnap] = useState<PortfolioSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    api.portfolioLatest()
      .then((s) => { setSnap(s); setErr(""); })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const available = snap?.available;
  // 快照 holdings 原序由引擎并发取价的返回先后决定（非确定性），
  // 这里按市值 CNY 降序固定展示顺序——大头在上，符合总控台直觉。
  const holdings = (snap?.holdings ?? [])
    .filter((h) => !h.error)
    .sort((a, b) => (b.market_value_cny ?? 0) - (a.market_value_cny ?? 0));
  // 加密货币单列成组，不再混入证券：type==="crypto" 归加密，其余归证券。
  const securities = holdings.filter((h) => h.type !== "crypto");
  const cryptos = holdings.filter((h) => h.type === "crypto");
  const cryptoTotal = snap?.crypto_total_cny ?? 0;
  const errored = (snap?.holdings ?? []).filter((h) => h.error);
  // 现金原序为 user_profile.json 书写顺序，同样按折 CNY 降序，与证券持仓口径一致。
  const cash = (snap?.cash_holdings ?? [])
    .slice()
    .sort((a, b) => (b.cny_value ?? 0) - (a.cny_value ?? 0));
  const suspectCount = holdings.filter((h) => h.suspect).length;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "1.25rem", flexWrap: "wrap" }}>
        <Link href="/" style={{ color: "var(--text-muted)", fontSize: 13 }}>← 返回总览</Link>
        <span style={{ color: "var(--border-hi)" }}>|</span>
        <h1 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)", margin: 0 }}>投资总控台</h1>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
          {snap?.generated_at && (
            <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
              快照时间：{snap.generated_at}
            </span>
          )}
          <button onClick={load} disabled={loading} className="tag-btn" style={{ fontSize: 12 }}>
            {loading ? "刷新中…" : "↻ 刷新"}
          </button>
        </div>
      </div>

      {err && <p style={{ color: "var(--red)", marginBottom: "1rem" }}>{err}</p>}

      {!loading && !available && !err && (
        <div className="card" style={{ padding: "1.25rem", color: "var(--text-muted)", fontSize: 13, lineHeight: 1.7 }}>
          暂无组合快照。盘后调度器（16:30）首次运行后即生成，或在 stock 容器内手动执行
          <code style={{ margin: "0 4px", color: "var(--text)", fontFamily: "var(--font-mono)" }}>python -m stock_bot.snapshot</code>
          生成一条。
        </div>
      )}

      {available && (
        <>
          {/* 时间窗切换（Ghostfolio 范式骨架，v1 仅「累计」点亮） */}
          <div style={{ display: "flex", gap: 6, marginBottom: "1.25rem", flexWrap: "wrap", rowGap: 6, alignItems: "center" }}>
            {TIME_WINDOWS.map((w) => {
              const active = w === "累计";
              return (
                <button
                  key={w}
                  disabled={!active}
                  className={`tag-btn${active ? " active" : ""}`}
                  style={{ fontSize: 11, opacity: active ? 1 : 0.4, cursor: active ? "pointer" : "not-allowed" }}
                  title={active ? "当前展示累计口径" : "趋势窗待快照积累（二档）"}
                >
                  {w}
                </button>
              );
            })}
            <span style={{ color: "var(--text-dim)", fontSize: 11, marginLeft: 4 }}>
              趋势窗待快照积累后点亮
            </span>
          </div>

          {/* KPI 卡（证券/加密/现金拆分由下方「资产配比」承载，此处不重复） */}
          <div style={{
            display: "grid",
            gridTemplateColumns: isMobile ? "1fr" : "repeat(2, minmax(0, 1fr))",
            gap: "1rem",
            marginBottom: "1.5rem",
          }}>
            <KpiCard title="总净值（含现金）">
              <span style={{ fontSize: 26, fontWeight: 800, color: "var(--text)", fontVariantNumeric: "tabular-nums" }}>
                {fmtCny(snap?.total_market_value)}
              </span>
              <span style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 4 }}>
                成本 {fmtCny(snap?.total_cost)}
              </span>
            </KpiCard>

            <KpiCard title="累计盈亏">
              <span style={{ fontSize: 26, fontWeight: 800, color: pnlColor(snap?.total_profit_loss), fontVariantNumeric: "tabular-nums" }}>
                {fmtCny(snap?.total_profit_loss)}
              </span>
              <span style={{ color: pnlColor(snap?.profit_loss_percent), fontSize: 13, fontWeight: 600, marginTop: 4 }}>
                {fmtPct(snap?.profit_loss_percent)}
              </span>
            </KpiCard>
          </div>

          {/* 配比：资产配比（证券/加密/现金）+ 币种敞口 */}
          <div style={{
            display: "grid",
            gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr",
            gap: "1rem",
            marginBottom: "1.5rem",
          }}>
            <AllocCard
              title="资产配比"
              segments={[
                { label: "证券", value: snap?.securities_total_cny ?? 0, color: "var(--blue)" },
                ...(cryptoTotal > 0 ? [{ label: "加密", value: cryptoTotal, color: "var(--teal)" }] : []),
                { label: "现金", value: snap?.cash_total_cny ?? 0, color: "var(--amber)" },
              ].sort((a, b) => b.value - a.value)}
            />
            <CurrencyExposureCard
              exposure={snap?.currency_exposure ?? {}}
              fxTrend={snap?.fx_trend ?? {}}
            />
          </div>

          {/* 异常盈亏哨兵告警 */}
          {suspectCount > 0 && (
            <div className="card" style={{ padding: "0.75rem 1rem", marginBottom: "1rem", borderColor: "var(--amber)" }}>
              <span style={{ color: "var(--amber)", fontSize: 12 }}>
                ⚠️ 有 {suspectCount} 笔持仓盈亏率绝对值 &gt; 90%，疑似取价异常，已在下表标注「待核实」——请先人工核实，勿据此加减仓。
              </span>
            </div>
          )}

          {/* 证券持仓明细 */}
          <SectionTitle>证券持仓（{securities.length}）</SectionTitle>
          <HoldingsTable holdings={securities} isMobile={isMobile} />

          {errored.length > 0 && (
            <p style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 6 }}>
              {errored.length} 笔取价失败：{errored.map((h) => h.ticker).join("、")}
            </p>
          )}

          {/* 加密货币明细（独立成组，不混入证券） */}
          {cryptos.length > 0 && (
            <>
              <SectionTitle>加密货币（{cryptos.length}）</SectionTitle>
              <HoldingsTable holdings={cryptos} isMobile={isMobile} />
            </>
          )}

          {/* 现金明细 */}
          {cash.length > 0 && (
            <>
              <SectionTitle>现金 / 活动资金（{cash.length}）</SectionTitle>
              <CashTable cash={cash} />
            </>
          )}

          <p style={{ color: "var(--text-dim)", fontSize: 11, marginTop: "1.5rem" }}>
            数据口径：CNY 折算 · 由 stock_bot 估值引擎每日落盘 · 最近更新 {fmtTime(snap?.generated_at)}
          </p>
        </>
      )}
    </div>
  );
}

function KpiCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card" style={{ padding: "0.85rem 1.1rem", display: "flex", flexDirection: "column", minWidth: 0 }}>
      <span style={{
        display: "block", color: "var(--text-muted)", fontSize: 11, fontWeight: 600,
        letterSpacing: "0.04em", textTransform: "uppercase", marginBottom: "0.5rem",
      }}>{title}</span>
      {children}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ color: "var(--text-muted)", fontSize: 13, fontWeight: 600, margin: "1.25rem 0 0.75rem" }}>
      {children}
    </div>
  );
}

function AllocCard({ title, segments }: {
  title: string;
  segments: { label: string; value: number; color: string }[];
}) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  return (
    <div className="card" style={{ padding: "0.85rem 1.1rem" }}>
      <span style={{
        display: "block", color: "var(--text-muted)", fontSize: 11, fontWeight: 600,
        letterSpacing: "0.04em", textTransform: "uppercase", marginBottom: "0.6rem",
      }}>{title}</span>
      <div style={{ display: "flex", height: 8, borderRadius: 4, overflow: "hidden", background: "var(--border)", marginBottom: "0.6rem" }}>
        {total > 0 && segments.map((s) => (
          <div key={s.label} style={{ width: `${(s.value / total) * 100}%`, background: s.color, transition: "width 0.5s var(--ease)" }} />
        ))}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {segments.map((s) => (
          <div key={s.label} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: s.color, flexShrink: 0 }} />
            <span style={{ color: "var(--text-muted)", flex: 1 }}>{s.label}</span>
            <span style={{ color: "var(--text)", fontVariantNumeric: "tabular-nums" }}>{fmtCny(s.value)}</span>
            <span style={{ color: "var(--text-dim)", minWidth: 42, textAlign: "right" }}>
              {total > 0 ? `${Math.round((s.value / total) * 100)}%` : "—"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 迷你走势线（内联 SVG，中性描边，仅示意方向，不抢色）。 */
function Sparkline({ data, width = 52, height = 14 }: { data: number[]; width?: number; height?: number }) {
  if (!data || data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pts = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * width;
      const y = height - ((v - min) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ display: "block", flexShrink: 0 }}>
      <polyline points={pts} fill="none" stroke="var(--text-dim)" strokeWidth={1} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/** 币种敞口卡：敞口配比条 + 每币种两行（敞口金额/占比 + 汇率趋势走势线/变化率）。 */
function CurrencyExposureCard({ exposure, fxTrend }: {
  exposure: Record<string, number>;
  fxTrend: Record<string, FxTrend>;
}) {
  const entries = Object.entries(exposure).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  return (
    <div className="card" style={{ padding: "0.85rem 1.1rem" }}>
      <span style={{
        display: "block", color: "var(--text-muted)", fontSize: 11, fontWeight: 600,
        letterSpacing: "0.04em", textTransform: "uppercase", marginBottom: "0.6rem",
      }}>币种敞口</span>
      <div style={{ display: "flex", height: 8, borderRadius: 4, overflow: "hidden", background: "var(--border)", marginBottom: "0.6rem" }}>
        {total > 0 && entries.map(([cur, val], i) => (
          <div key={cur} style={{ width: `${(val / total) * 100}%`, background: ALLOC_PALETTE[i % ALLOC_PALETTE.length], transition: "width 0.5s var(--ease)" }} />
        ))}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {entries.map(([cur, val], i) => {
          const t = fxTrend[cur];
          const pct = total > 0 ? Math.round((val / total) * 100) : 0;
          return (
            <div key={cur}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
                <span style={{ width: 10, height: 10, borderRadius: 2, background: ALLOC_PALETTE[i % ALLOC_PALETTE.length], flexShrink: 0 }} />
                <span style={{ color: "var(--text-muted)", flex: 1 }}>{CURRENCY_LABEL[cur] ?? cur}</span>
                <span style={{ color: "var(--text)", fontVariantNumeric: "tabular-nums" }}>{fmtCny(val)}</span>
                <span style={{ color: "var(--text-dim)", minWidth: 42, textAlign: "right" }}>{total > 0 ? `${pct}%` : "—"}</span>
              </div>
              {t && (
                <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11, marginTop: 3, paddingLeft: 18 }}>
                  <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{t.rate}</span>
                  <Sparkline data={t.spark ?? []} />
                  <span style={{ color: pnlColor(t.change_pct), fontVariantNumeric: "tabular-nums" }}>
                    {t.change_pct >= 0 ? "▲" : "▼"}{fmtPct(t.change_pct)}
                  </span>
                  <span style={{ color: "var(--text-dim)" }}>近{t.window_days ?? 7}日</span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function HoldingsTable({ holdings, isMobile }: { holdings: PortfolioHolding[]; isMobile: boolean }) {
  if (holdings.length === 0) {
    return <p style={{ color: "var(--text-dim)", fontSize: 12 }}>暂无持仓</p>;
  }
  const totalMv = holdings.reduce((s, h) => s + (h.market_value_cny ?? 0), 0);
  return (
    <div className="card" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: isMobile ? 876 : 956, tableLayout: "fixed" }}>
          <colgroup>
            <col style={{ width: 150 }} />
            <col style={{ width: 76 }} />
            <col style={{ width: 96 }} />
            <col style={{ width: 120 }} />
            <col style={{ width: 116 }} />
            <col style={{ width: 116 }} />
            <col style={{ width: 116 }} />
            <col style={{ width: 90 }} />
            <col style={{ width: 70 }} />
          </colgroup>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              {["持仓", "数量", "现价", "原币市值", "市值(CNY)", "成本(CNY)", "盈亏(CNY)", "盈亏%", "占比"].map((h, i) => (
                <th key={h} style={{ padding: "8px 12px", textAlign: i === 0 ? "left" : "right", color: "var(--text-muted)", fontWeight: 600, whiteSpace: "nowrap" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {holdings.map((h, i) => {
              const pct = totalMv > 0 ? Math.round((h.market_value_cny ?? 0) / totalMv * 100) : 0;
              return (
                <tr key={h.ticker} style={{ borderBottom: i < holdings.length - 1 ? "1px solid var(--border)" : undefined }}>
                  <td style={{ padding: "8px 12px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={`${h.company_name} ${h.ticker}`}>
                    <span style={{ color: "var(--text)", fontWeight: 600, fontFamily: "var(--font-mono)" }}>{h.ticker}</span>
                    {h.suspect && <span style={{ color: "var(--amber)", marginLeft: 6 }}>⚠️待核实</span>}
                    <span style={{ color: "var(--text-dim)", marginLeft: 6, fontSize: 11 }}>{h.company_name}</span>
                  </td>
                  <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{h.shares}</td>
                  <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                    {h.currency_symbol}{h.current_price?.toLocaleString("en-US", { maximumFractionDigits: 2 })}
                  </td>
                  <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                    {h.currency_symbol}{h.native_market_value?.toLocaleString("en-US", { maximumFractionDigits: 2 })}
                  </td>
                  <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text)", fontFamily: "var(--font-mono)" }}>{fmtCny(h.market_value_cny)}</td>
                  <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{fmtCny(h.cost_value_cny)}</td>
                  <td style={{ padding: "8px 12px", textAlign: "right", color: pnlColor(h.profit_loss_cny), fontFamily: "var(--font-mono)" }}>{fmtCny(h.profit_loss_cny)}</td>
                  <td style={{ padding: "8px 12px", textAlign: "right", color: pnlColor(h.profit_loss_percent), fontFamily: "var(--font-mono)", fontWeight: 600 }}>{fmtPct(h.profit_loss_percent)}</td>
                  <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-dim)" }}>{pct}%</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CashTable({ cash }: { cash: PortfolioCashHolding[] }) {
  return (
    <div className="card" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 420 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              {["平台", "金额", "币种", "折 CNY"].map((h, i) => (
                <th key={h} style={{ padding: "8px 12px", textAlign: i === 0 ? "left" : "right", color: "var(--text-muted)", fontWeight: 600, whiteSpace: "nowrap" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {cash.map((c, i) => (
              <tr key={c.platform} style={{ borderBottom: i < cash.length - 1 ? "1px solid var(--border)" : undefined }}>
                <td style={{ padding: "8px 12px", color: "var(--text)" }}>{c.platform}</td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{c.amount.toLocaleString()}</td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-dim)" }}>{CURRENCY_LABEL[c.currency] ?? c.currency}</td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--amber)", fontFamily: "var(--font-mono)" }}>{fmtCny(c.cny_value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

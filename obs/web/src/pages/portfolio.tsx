import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/router";
import {
  api,
  type PortfolioSnapshot,
  type PortfolioHolding,
  type PortfolioCashHolding,
  type PortfolioHistoryPoint,
  type FxTrend,
  type WatchlistItem,
} from "@/lib/api";
import { fmtTime, fmtPct, pnlColor } from "@/lib/format";
import { useIsMobile } from "@/lib/useIsMobile";
import { StockTrendModal } from "@/components/StockTrendModal";
import { StockScreenerPanel } from "@/components/StockScreenerPanel";

type PortfolioTab = "holdings" | "screener";

/** ¥ 金额格式（千分位，两位小数）。 */
function fmtCny(n: number | undefined | null): string {
  if (n === undefined || n === null) return "—";
  return `¥${n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

const CURRENCY_LABEL: Record<string, string> = { USD: "美元", HKD: "港币", CNY: "人民币" };
const ALLOC_PALETTE = ["var(--cat-1)", "var(--cat-2)", "var(--cat-3)", "var(--blue)", "var(--amber)"];

// Ghostfolio 范式的时间窗；今日/累计已点亮（有基准），MTD/YTD/1Y 待快照与现金流积累（二档）。
const TIME_WINDOWS = ["今日", "WTD", "MTD", "YTD", "1Y", "累计"] as const;
const ACTIVE_WINDOWS = new Set<string>(["今日", "累计"]);

export default function PortfolioPage() {
  const isMobile = useIsMobile();
  const router = useRouter();
  const [tab, setTab] = useState<PortfolioTab>("holdings");
  const [snap, setSnap] = useState<PortfolioSnapshot | null>(null);
  const [history, setHistory] = useState<PortfolioHistoryPoint[]>([]);
  const [historyExcludedCount, setHistoryExcludedCount] = useState(0);
  const [win, setWin] = useState<"今日" | "累计">("累计");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [revaluing, setRevaluing] = useState(false);
  const [revalMsg, setRevalMsg] = useState("");
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [watchMsg, setWatchMsg] = useState("");
  const [watchAddText, setWatchAddText] = useState("");
  const [watchAdding, setWatchAdding] = useState(false);

  // 支持从总览页深链直接落到「选股」tab（?tab=screener），仅用于设置初始态。
  useEffect(() => {
    if (router.isReady && router.query.tab === "screener") {
      setTab("screener");
    }
  }, [router.isReady, router.query.tab]);

  const load = useCallback(() => {
    setLoading(true);
    Promise.all([api.portfolioLatest(), api.portfolioHistory(90)])
      .then(([s, h]) => {
        setSnap(s);
        setHistory(h.points ?? []);
        setHistoryExcludedCount(h.excluded_count ?? h.excluded_points?.length ?? 0);
        setErr("");
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  // 「重新估值」：触发 stock bot 联网重新取价并覆盖当天快照，跑完重读。
  // 区别于「刷新」（纯重读已落盘数据、秒回）。60s 冷却由 bot 侧强制，前端只做提示。
  const revalue = useCallback(() => {
    setRevaluing(true);
    setRevalMsg("");
    api.portfolioRefresh()
      .then((r) => {
        setRevalMsg(`✓ 已重新取价（净值 ${fmtCny(r.total_market_value)}）`);
        load();
      })
      .catch((e) => {
        const msg = String(e);
        setRevalMsg(msg.includes("429") ? "⏳ 冷却中，请约 1 分钟后再试" : `重新估值失败：${msg}`);
      })
      .finally(() => setRevaluing(false));
  }, [load]);

  // 观察清单读走 obs 直读挂载文件（秒回、不依赖 live bot），与快照分开加载，
  // 便于选股页加入后单独刷新，不必重拉整份组合快照。
  const loadWatchlist = useCallback(() => {
    api.watchlist()
      .then((r) => setWatchlist(r.items ?? []))
      .catch(() => {});
  }, []);

  // 加入观察清单（选股页「+ 观察」与本页「手动添加」共用）：写走 live bot 代理，
  // 成功后刷新本页观察清单。返回后端状态供选股页行内反馈。
  const addWatch = useCallback(
    async (ticker: string): Promise<string> => {
      const r = await api.watchlistAdd(ticker);
      setWatchlist(r.items ?? []);
      if (r.status === "full") setWatchMsg("观察清单已达上限（100），请先移除部分标的");
      else if (r.status === "invalid") setWatchMsg("代码无效");
      else setWatchMsg("");
      return r.status;
    },
    [],
  );

  const removeWatch = useCallback((ticker: string) => {
    api.watchlistRemove(ticker)
      .then((r) => setWatchlist(r.items ?? []))
      .catch((e) => setWatchMsg(`移除失败：${String(e)}`));
  }, []);

  const manualAddWatch = useCallback(() => {
    const t = watchAddText.trim();
    if (!t) return;
    setWatchAdding(true);
    addWatch(t)
      .then((status) => { if (status === "ok" || status === "exists") setWatchAddText(""); })
      .catch((e) => setWatchMsg(`添加失败：${String(e)}`))
      .finally(() => setWatchAdding(false));
  }, [watchAddText, addWatch]);

  useEffect(() => { load(); loadWatchlist(); }, [load, loadWatchlist]);

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
  // 已实现盈亏（平仓落袋，按快照当日汇率折 CNY）；历史快照无此字段，?? 0 兜底。
  const realizedPnl = snap?.realized_pnl_total_cny ?? 0;
  const combinedPnl =
    snap?.total_profit_loss === undefined || snap?.total_profit_loss === null
      ? undefined
      : snap.total_profit_loss + realizedPnl;
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
        {tab === "holdings" && (
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
            {snap?.generated_at && (
              <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
                快照时间：{snap.generated_at}
              </span>
            )}
            <button onClick={load} disabled={loading} className="tag-btn" style={{ fontSize: 12 }}>
              {loading ? "刷新中…" : "↻ 刷新"}
            </button>
            <button
              onClick={revalue}
              disabled={revaluing || loading}
              className="tag-btn"
              style={{ fontSize: 12 }}
              title="触发 stock bot 联网重新取价并覆盖当天快照（约十几秒，60s 冷却）"
            >
              {revaluing ? "重新估值中…" : "⟳ 重新估值"}
            </button>
          </div>
        )}
      </div>

      {/* 页内 Tab：持仓（原有总控台内容）/ 选股（批量筛股，见 StockScreenerPanel） */}
      <div style={{ display: "flex", gap: 6, marginBottom: "1.25rem" }}>
        <button onClick={() => setTab("holdings")} className={`tag-btn${tab === "holdings" ? " active" : ""}`} style={{ fontSize: 12 }}>
          持仓
        </button>
        <button onClick={() => setTab("screener")} className={`tag-btn${tab === "screener" ? " active" : ""}`} style={{ fontSize: 12 }}>
          选股
        </button>
      </div>

      {tab === "screener" && <StockScreenerPanel onSelectTicker={setSelectedTicker} onAddWatch={addWatch} />}

      {tab === "holdings" && (
        <>
      {revalMsg && (
        <p style={{ color: revalMsg.startsWith("✓") ? "var(--text-muted)" : "var(--amber)", fontSize: 12, marginTop: -8, marginBottom: "1rem" }}>
          {revalMsg}
        </p>
      )}

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
          {/* 时间窗切换（今日/累计已点亮，MTD/YTD/1Y 待积累） */}
          <div style={{ display: "flex", gap: 6, marginBottom: "1rem", flexWrap: "wrap", rowGap: 6, alignItems: "center" }}>
            {TIME_WINDOWS.map((w) => {
              const active = ACTIVE_WINDOWS.has(w);
              const selected = active && w === win;
              return (
                <button
                  key={w}
                  disabled={!active}
                  onClick={() => active && setWin(w as "今日" | "累计")}
                  className={`tag-btn${selected ? " active" : ""}`}
                  style={{ fontSize: 11, opacity: active ? 1 : 0.4, cursor: active ? "pointer" : "not-allowed" }}
                  title={active ? "查看该窗净值变化" : "趋势窗待快照与现金流积累（二档）"}
                >
                  {w}
                </button>
              );
            })}
            <span style={{ color: "var(--text-dim)", fontSize: 11, marginLeft: 4 }}>
              MTD/YTD/1Y 待快照与现金流积累后点亮
            </span>
          </div>

          {/* 净值走势曲线（Ghostfolio Net Worth Chart：纯总资产走势，不扣现金流） */}
          <NetWorthCard
            points={win === "今日" ? history.slice(-2) : history}
            win={win}
            excludedCount={historyExcludedCount}
          />

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

            <KpiCard title="累计盈亏（浮动 + 已实现）">
              <span style={{ fontSize: 26, fontWeight: 800, color: pnlColor(combinedPnl), fontVariantNumeric: "tabular-nums" }}>
                {fmtCny(combinedPnl)}
              </span>
              <span style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 4 }}>
                浮动{" "}
                <span style={{ color: pnlColor(snap?.total_profit_loss), fontWeight: 600 }}>
                  {fmtCny(snap?.total_profit_loss)}（{fmtPct(snap?.profit_loss_percent)}）
                </span>
                {" · "}已实现{" "}
                <span
                  style={{ color: pnlColor(realizedPnl), fontWeight: 600 }}
                  title="历史减仓/清仓落袋部分，按快照当日汇率折算；未换汇的外币回款会随汇率微动"
                >
                  {fmtCny(realizedPnl)}
                </span>
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
          <HoldingsTable holdings={securities} isMobile={isMobile} onSelect={setSelectedTicker} />

          {errored.length > 0 && (
            <p style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 6 }}>
              {errored.length} 笔取价失败：{errored.map((h) => h.ticker).join("、")}
            </p>
          )}

          {/* 加密货币明细（独立成组，不混入证券） */}
          {cryptos.length > 0 && (
            <>
              <SectionTitle>加密货币（{cryptos.length}）</SectionTitle>
              <HoldingsTable holdings={cryptos} isMobile={isMobile} onSelect={setSelectedTicker} />
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

      {/* 👀 观察清单：0 持仓的纯跟踪位，与净值/估值解耦，故置于 available 门控之外——
          就算没有任何持仓快照也能浏览关注的公司。点击行进入趋势分析（复用 StockTrendModal）。 */}
      <div style={{ marginTop: available ? "2rem" : 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "1.25rem 0 0.75rem", flexWrap: "wrap" }}>
          <span style={{ color: "var(--text-muted)", fontSize: 13, fontWeight: 600 }}>
            👀 观察清单（{watchlist.length}）
          </span>
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>未持仓，不计入净值 · 点击查看趋势分析</span>
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
            <input
              value={watchAddText}
              onChange={(e) => setWatchAddText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") manualAddWatch(); }}
              placeholder="代码，如 NVDA / 0700.HK"
              style={{
                width: 168, background: "var(--bg)", color: "var(--text)", border: "1px solid var(--border)",
                borderRadius: "var(--r-sm)", padding: "5px 8px", fontSize: 12, fontFamily: "var(--font-mono)",
              }}
            />
            <button onClick={manualAddWatch} disabled={watchAdding || !watchAddText.trim()} className="tag-btn" style={{ fontSize: 12 }}>
              {watchAdding ? "添加中…" : "+ 添加"}
            </button>
          </div>
        </div>
        {watchMsg && <p style={{ color: "var(--amber)", fontSize: 11, margin: "0 0 8px" }}>{watchMsg}</p>}
        <WatchlistTable items={watchlist} onSelect={setSelectedTicker} onRemove={removeWatch} />
      </div>
        </>
      )}

      <StockTrendModal ticker={selectedTicker} onClose={() => setSelectedTicker(null)} />
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

/** 净值走势卡：标题 + 选中窗的净值变化（非投资收益）+ 全宽走势曲线。 */
function NetWorthCard({ points, win, excludedCount }: {
  points: PortfolioHistoryPoint[];
  win: string;
  excludedCount: number;
}) {
  const enough = points.length >= 2;
  const first = points[0]?.total_market_value ?? 0;
  const last = points[points.length - 1]?.total_market_value ?? 0;
  const delta = last - first;
  const deltaPct = first > 0 ? (delta / first) * 100 : 0;
  return (
    <div className="card" style={{ padding: "0.85rem 1.1rem", marginBottom: "1.5rem" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
        <span style={{
          color: "var(--text-muted)", fontSize: 11, fontWeight: 600,
          letterSpacing: "0.04em", textTransform: "uppercase",
        }}>净值走势 · {win}</span>
        {enough && (
          <>
            <span style={{ color: pnlColor(delta), fontSize: 18, fontWeight: 800, fontVariantNumeric: "tabular-nums" }}>
              {delta >= 0 ? "+" : "-"}{fmtCny(Math.abs(delta))}
            </span>
            <span style={{ color: pnlColor(deltaPct), fontSize: 12, fontWeight: 600 }}>{fmtPct(deltaPct)}</span>
          </>
        )}
        <span style={{ color: "var(--text-dim)", fontSize: 11, marginLeft: "auto" }}>
          {points.length} 个快照点 · 净值变化 ≠ 投资收益（未扣入金/加仓）
        </span>
      </div>
      {excludedCount > 0 && (
        <p style={{ color: "var(--amber)", fontSize: 11, margin: "0 0 8px" }}>
          已剔除 {excludedCount} 个取价异常快照，避免缺失持仓造成净值假跳变。
        </p>
      )}
      {enough ? (
        <>
          <NetWorthChart points={points} />
          {/* 起止日期 + 区间极值（HTML 渲染，避免 SVG 非等比缩放拉伸文字）。 */}
          <div style={{ display: "flex", alignItems: "center", fontSize: 10, color: "var(--text-dim)", marginTop: 4 }}>
            <span>{points[0].date}</span>
            <span style={{ margin: "0 auto", fontVariantNumeric: "tabular-nums" }}>
              区间 {fmtCny(Math.min(...points.map((p) => p.total_market_value)))} ~ {fmtCny(Math.max(...points.map((p) => p.total_market_value)))}
            </span>
            <span>{points[points.length - 1].date}</span>
          </div>
        </>
      ) : (
        <p style={{ color: "var(--text-dim)", fontSize: 12, margin: "8px 0" }}>快照不足两天，走势待积累。</p>
      )}
    </div>
  );
}

/** 净值走势曲线（内联 SVG，涨红跌绿；自然日等距、休市走平；无第三方图表库）。 */
function NetWorthChart({ points }: { points: PortfolioHistoryPoint[] }) {
  const W = 640, H = 150, padT = 10, padB = 8;
  const vals = points.map((p) => p.total_market_value);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const x = (i: number) => (i / (points.length - 1)) * W;
  const y = (v: number) => padT + (1 - (v - min) / range) * (H - padT - padB);
  const linePts = points.map((p, i) => `${x(i).toFixed(1)},${y(p.total_market_value).toFixed(1)}`).join(" ");
  const areaPts = `0,${H} ${linePts} ${W},${H}`;
  const up = vals[vals.length - 1] >= vals[0];
  const color = up ? "var(--red)" : "var(--green)"; // 涨红跌绿（A 股语义，与 pnlColor 一致）
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ display: "block", width: "100%", height: 150 }}>
      <polygon points={areaPts} fill={color} fillOpacity={0.08} />
      <polyline points={linePts} fill="none" stroke={color} strokeWidth={1.6} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      <circle cx={x(points.length - 1)} cy={y(vals[vals.length - 1])} r={3} fill={color} vectorEffect="non-scaling-stroke" />
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

function HoldingsTable({ holdings, isMobile, onSelect }: {
  holdings: PortfolioHolding[];
  isMobile: boolean;
  onSelect?: (ticker: string) => void;
}) {
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
                <tr
                  key={h.ticker}
                  onClick={() => onSelect?.(h.ticker)}
                  style={{
                    borderBottom: i < holdings.length - 1 ? "1px solid var(--border)" : undefined,
                    cursor: onSelect ? "pointer" : undefined,
                  }}
                >
                  <td style={{ padding: "8px 12px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={`${h.company_name} ${h.ticker} · 点击查看趋势分析`}>
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

/** 观察清单表：代码 + 备注 + 加入日期，点击行进入趋势分析，行尾可移除。 */
function WatchlistTable({ items, onSelect, onRemove }: {
  items: WatchlistItem[];
  onSelect: (ticker: string) => void;
  onRemove: (ticker: string) => void;
}) {
  if (items.length === 0) {
    return (
      <p style={{ color: "var(--text-dim)", fontSize: 12 }}>
        暂无观察标的。在「选股」页命中的标的点「+ 观察」，或用上方输入框手动添加。
      </p>
    );
  }
  return (
    <div className="card" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 420 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              {["代码", "备注", "加入日期", ""].map((h, i) => (
                <th key={i} style={{ padding: "8px 12px", textAlign: i === 0 ? "left" : i === 3 ? "right" : "left", color: "var(--text-muted)", fontWeight: 600, whiteSpace: "nowrap" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.map((it, i) => (
              <tr
                key={it.ticker}
                onClick={() => onSelect(it.ticker)}
                style={{ borderBottom: i < items.length - 1 ? "1px solid var(--border)" : undefined, cursor: "pointer" }}
                title="点击查看趋势分析"
              >
                <td style={{ padding: "8px 12px", color: "var(--text)", fontWeight: 600, fontFamily: "var(--font-mono)" }}>{it.ticker}</td>
                <td style={{ padding: "8px 12px", color: "var(--text-dim)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 220 }}>{it.note || "—"}</td>
                <td style={{ padding: "8px 12px", color: "var(--text-dim)", fontFamily: "var(--font-mono)" }}>{it.added_at || "—"}</td>
                <td style={{ padding: "8px 12px", textAlign: "right" }}>
                  <button
                    onClick={(e) => { e.stopPropagation(); onRemove(it.ticker); }}
                    className="tag-btn"
                    style={{ fontSize: 11 }}
                    title="从观察清单移除"
                  >
                    移除
                  </button>
                </td>
              </tr>
            ))}
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

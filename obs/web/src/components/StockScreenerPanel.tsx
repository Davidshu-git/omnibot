import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ScreenerStatus, type ScreenerResult } from "@/lib/api";
import { fmtPct, pnlColor } from "@/lib/format";
import { useIsMobile } from "@/lib/useIsMobile";

const POLL_INTERVAL_MS = 3000;
// 兜底：轮询这么久还没结束就提示"可能仍在跑"，不强制中断——大股票池扫描本就可能要几分钟。
const POLL_STALL_HINT_MS = 90_000;

/** 选股筛股面板：股票池编辑 + 启动扫描 + 进度轮询 + 结果列表。
 * 嵌入投资总控台「选股」tab；命中的标的点击后交由调用方复用共享的 StockTrendModal；
 * 每行「+ 观察」把标的加入自选观察清单（onAddWatch 由持仓页提供，落 watchlist.json）。 */
export function StockScreenerPanel({
  onSelectTicker,
  onAddWatch,
}: {
  onSelectTicker: (ticker: string) => void;
  /** 加入观察清单；返回后端状态（ok/exists/full/…）供行内反馈。缺省则不显示「+ 观察」列。 */
  onAddWatch?: (ticker: string) => Promise<string>;
}) {
  const isMobile = useIsMobile();
  // 已加入 / 正在加入的标的（行内按钮反馈），随本次扫描结果生命周期存在。
  const [addedTickers, setAddedTickers] = useState<Record<string, boolean>>({});
  const [pendingTickers, setPendingTickers] = useState<Record<string, boolean>>({});

  const handleAddWatch = useCallback(
    async (ticker: string) => {
      if (!onAddWatch) return;
      setPendingTickers((p) => ({ ...p, [ticker]: true }));
      try {
        const status = await onAddWatch(ticker);
        if (status === "ok" || status === "exists") {
          setAddedTickers((a) => ({ ...a, [ticker]: true }));
        }
      } catch {
        // 失败提示交由父组件的 toast，行内按钮恢复可点。
      } finally {
        setPendingTickers((p) => {
          const next = { ...p };
          delete next[ticker];
          return next;
        });
      }
    },
    [onAddWatch],
  );
  const [universeText, setUniverseText] = useState("");
  const [savingUniverse, setSavingUniverse] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");
  const [status, setStatus] = useState<ScreenerStatus | null>(null);
  // 「逆小势」收敛：默认只看回调观察标的（顺大势入围里正在被错杀的短名单），一键切回全量。
  const [onlyPullback, setOnlyPullback] = useState(true);
  // 「势能优先」排序：先看逆市抗跌强度、同强度再看真实趋势年限（呼应"顺势而为"中的"势=强度"）。
  const [sortByPotential, setSortByPotential] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startMsg, setStartMsg] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollStartedAtRef = useRef<number>(0);

  const loadUniverse = useCallback(() => {
    api.screenerUniverse()
      .then((r) => setUniverseText((r.tickers ?? []).join("\n")))
      .catch(() => {});
  }, []);

  const loadStatus = useCallback(() => {
    api.screenerStatus()
      .then((s) => setStatus(s))
      .catch((e) => setStatus({ status: "error", error: String(e) }));
  }, []);

  useEffect(() => {
    loadUniverse();
    loadStatus();
  }, [loadUniverse, loadStatus]);

  // 扫描进行中按 3s 轮询状态（同 executor-instances.tsx 电源开关的轮询范式），
  // 完成/出错即停止；90s 无变化只是提示，不强制打断（扫描仍可能在后台继续跑）。
  useEffect(() => {
    const running = status?.status === "running";
    if (running && !pollRef.current) {
      pollStartedAtRef.current = Date.now();
      pollRef.current = setInterval(loadStatus, POLL_INTERVAL_MS);
    }
    if (!running && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [status?.status, loadStatus]);

  const saveUniverse = useCallback(() => {
    const tickers = universeText.split("\n").map((t) => t.trim()).filter(Boolean);
    setSavingUniverse(true);
    setSaveMsg("");
    api.screenerUniverseSave(tickers)
      .then((r) => {
        setUniverseText((r.tickers ?? []).join("\n"));
        setSaveMsg(`✓ 已保存 ${r.tickers?.length ?? 0} 只代码`);
      })
      .catch((e) => setSaveMsg(`保存失败：${String(e)}`))
      .finally(() => setSavingUniverse(false));
  }, [universeText]);

  const [loadingPreset, setLoadingPreset] = useState(false);
  const [presetMsg, setPresetMsg] = useState("");

  // 只填文本框，不自动保存——用户仍需显式点「保存股票池」才会覆盖 universe.json，
  // 给一次反悔/精简的机会（预置池约 4875 只，全量扫描耗时会很长）。
  const loadPreset = useCallback(() => {
    setLoadingPreset(true);
    setPresetMsg("");
    api.screenerPreset()
      .then((r) => {
        const tickers = r.tickers ?? [];
        setUniverseText(tickers.join("\n"));
        setPresetMsg(`✓ 已载入 ${tickers.length} 只精选股（标普500+恒生科技）到文本框，点「保存股票池」才会生效`);
      })
      .catch((e) => setPresetMsg(`载入失败：${String(e)}`))
      .finally(() => setLoadingPreset(false));
  }, []);

  const startScan = useCallback(() => {
    setStarting(true);
    setStartMsg("");
    api.screenerStart()
      .then((r) => {
        setStartMsg(r.status === "already_running" ? "⏳ 已有扫描在跑，请等待完成" : "");
        loadStatus();
      })
      .catch((e) => setStartMsg(`启动失败：${String(e)}`))
      .finally(() => setStarting(false));
  }, [loadStatus]);

  const running = status?.status === "running";
  const done = status?.status === "done";
  const total = status?.total ?? 0;
  const pct = running && total > 0 ? Math.round(((status?.done ?? 0) / total) * 100) : 0;
  const stalled = running && Date.now() - pollStartedAtRef.current > POLL_STALL_HINT_MS;
  const results = status?.results ?? []; // 后端已按相对强度降序排好
  const pullbackCount = results.filter((r) => r.pullback_watch).length;
  const filteredResults = onlyPullback ? results.filter((r) => r.pullback_watch) : results;
  // 势能优先：逆市强度降序（None 垫底），同强度按真实趋势年限降序；否则保留后端相对强度降序。
  const shownResults = sortByPotential
    ? [...filteredResults].sort((a, b) => {
        const cs = (b.counter_trend_strength ?? -Infinity) - (a.counter_trend_strength ?? -Infinity);
        return cs !== 0 ? cs : (b.trend_duration_days ?? -1) - (a.trend_duration_days ?? -1);
      })
    : filteredResults;
  // 跳过项按原因归类成一行汇总（533 池会跳过数百只，逐只铺开是噪音，明细收进折叠区）。
  const skippedReasonSummary = Object.entries(
    (status?.skipped ?? []).reduce<Record<string, number>>((acc, s) => {
      acc[s.skip_reason] = (acc[s.skip_reason] ?? 0) + 1;
      return acc;
    }, {}),
  )
    .sort((a, b) => b[1] - a[1])
    .map(([reason, n]) => `${reason} ${n}`)
    .join("、");
  const universeCount = universeText.split("\n").map((t) => t.trim()).filter(Boolean).length;

  return (
    <div>
      <p style={{ color: "var(--text-dim)", fontSize: 12, marginBottom: "1rem", lineHeight: 1.7 }}>
        硬性过滤：长周期均线（MA250）必须向上（"大钟摆昂首向上"）。通过后按相对强度（近 60 个交易日跑赢/跑输对应基准指数的幅度）降序展示，
        附「逆市强度」（只在大盘下跌日衡量的抗跌/逆势能力）、「真实趋势年限」（MA250 连涨年数，取数窗口 6 年）、偏离度历史分位——「势能优先」开关按"顺势而为（势=强度）"重排。
        「回调观察」列标记"顺大势逆小势"特征——年线向上前提下现价跌破 MA60、仍在年线上方、且 MA60 偏离度处自身历史低位（恐慌钟摆到底）。
        仅供缩小候选范围与盯盘，<b>不构成买卖建议</b>（回调也可能一路破位、接飞刀风险仍在），选出来的标的仍需自行做基本面研究。加密货币不纳入筛选（大盘/行业联动等概念对其无对应意义）。
      </p>

      <div className="card" style={{ padding: "0.85rem 1.1rem", marginBottom: "1.5rem" }}>
        <span style={{
          display: "block", color: "var(--text-muted)", fontSize: 11, fontWeight: 600,
          letterSpacing: "0.04em", textTransform: "uppercase", marginBottom: "0.6rem",
        }}>股票池（每行一个代码）</span>
        <textarea
          value={universeText}
          onChange={(e) => setUniverseText(e.target.value)}
          rows={8}
          placeholder={"AAPL\nNVDA\n0700.HK\n600519"}
          style={{
            width: "100%", background: "var(--bg)", color: "var(--text)", border: "1px solid var(--border)",
            borderRadius: "var(--r-sm)", padding: "0.6rem", fontSize: 12, fontFamily: "var(--font-mono)", resize: "vertical",
          }}
        />
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10, flexWrap: "wrap" }}>
          <button
            onClick={loadPreset}
            disabled={loadingPreset}
            className="tag-btn"
            style={{ fontSize: 12 }}
            title="标普 500 + 恒生科技指数成分股（约 533 只，随版本库打包的静态快照，定期手动重生成），仅填文本框不自动保存"
          >
            {loadingPreset ? "载入中…" : "载入精选池（标普500+恒生科技）"}
          </button>
          <button onClick={saveUniverse} disabled={savingUniverse} className="tag-btn" style={{ fontSize: 12 }}>
            {savingUniverse ? "保存中…" : "保存股票池"}
          </button>
          <button onClick={startScan} disabled={starting || running} className="tag-btn" style={{ fontSize: 12 }}>
            {running ? "扫描中…" : starting ? "启动中…" : "开始扫描"}
          </button>
          {presetMsg && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{presetMsg}</span>}
          {saveMsg && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{saveMsg}</span>}
          {startMsg && <span style={{ color: "var(--amber)", fontSize: 11 }}>{startMsg}</span>}
        </div>
        {universeCount > 300 && (
          <p style={{ color: "var(--amber)", fontSize: 11, margin: "8px 0 0" }}>
            当前股票池 {universeCount} 只，扫描引擎无更细粒度限流，标的数较多时可能耗时数十分钟，也可能触发数据源限流导致部分标的取数失败——建议先精简再扫描，或耐心等待。
          </p>
        )}
      </div>

      {running && (
        <div className="card" style={{ padding: "0.85rem 1.1rem", marginBottom: "1.5rem" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
            <span style={{ color: "var(--text-muted)", fontSize: 12 }}>扫描中 {status?.done ?? 0}/{total}</span>
            <span style={{ color: "var(--text-dim)", fontSize: 11, marginLeft: "auto" }}>{pct}%</span>
          </div>
          <div style={{ height: 6, borderRadius: 3, background: "var(--border)", overflow: "hidden" }}>
            <div style={{ width: `${pct}%`, height: "100%", background: "var(--blue)", transition: "width 0.3s var(--ease)" }} />
          </div>
          {stalled && (
            <p style={{ color: "var(--amber)", fontSize: 11, margin: "8px 0 0" }}>
              轮询超过 90s 无变化，标的较多时扫描仍可能在后台继续运行（未强制中断），可稍后回来查看。
            </p>
          )}
        </div>
      )}

      {status?.status === "error" && (
        <div className="card" style={{ padding: "0.75rem 1rem", marginBottom: "1rem", borderColor: "var(--red)" }}>
          <span style={{ color: "var(--red)", fontSize: 12 }}>扫描出错：{status.error}</span>
        </div>
      )}

      {done && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", margin: "1.25rem 0 0.75rem" }}>
            <span style={{ color: "var(--text-muted)", fontSize: 13, fontWeight: 600 }}>
              {onlyPullback
                ? `筛选结果 · 回调观察（${pullbackCount} / 通过 ${status?.passed_count ?? 0}）`
                : `筛选结果（${status?.passed_count ?? 0} / ${total}）`}
            </span>
            <button
              onClick={() => setSortByPotential((v) => !v)}
              className={`tag-btn${sortByPotential ? " active" : ""}`}
              style={{ fontSize: 11, marginLeft: "auto" }}
              title="势能优先：先按逆市抗跌强度降序、同强度再按真实趋势年限降序（『顺势而为』——势=强度）。关闭则按相对强度降序。"
            >
              {sortByPotential ? "✓ 势能优先" : "势能优先"}
            </button>
            <button
              onClick={() => setOnlyPullback((v) => !v)}
              className={`tag-btn${onlyPullback ? " active" : ""}`}
              style={{ fontSize: 11 }}
              title="只保留『顺大势』入围里正在回调被错杀的短名单（跌破 MA60、仍在年线上方、MA60 偏离度处历史低位）"
            >
              {onlyPullback ? "✓ 只看回调观察" : "只看回调观察"}
            </button>
          </div>
          {results.length === 0 ? (
            <p style={{ color: "var(--text-dim)", fontSize: 12 }}>无标的通过硬性过滤（年线向上）</p>
          ) : shownResults.length === 0 ? (
            <p style={{ color: "var(--text-dim)", fontSize: 12 }}>
              本次 {status?.passed_count ?? 0} 只通过标的均不在回调观察区（可关闭上方开关查看全部通过标的）。
            </p>
          ) : (
            <ResultsTable
              results={shownResults}
              isMobile={isMobile}
              onSelect={onSelectTicker}
              onAddWatch={onAddWatch ? handleAddWatch : undefined}
              addedTickers={addedTickers}
              pendingTickers={pendingTickers}
            />
          )}

          {status?.skipped && status.skipped.length > 0 && (
            <details style={{ marginTop: 10 }}>
              <summary style={{ color: "var(--text-dim)", fontSize: 11, cursor: "pointer" }}>
                {status.skipped.length} 只被过滤/跳过（{skippedReasonSummary}）
              </summary>
              <p style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 6, lineHeight: 1.7 }}>
                {status.skipped.map((s) => `${s.ticker}${s.name ? "·" + s.name : ""}(${s.skip_reason})`).join("、")}
              </p>
            </details>
          )}
        </>
      )}
    </div>
  );
}

function ResultsTable({ results, isMobile, onSelect, onAddWatch, addedTickers, pendingTickers }: {
  results: ScreenerResult[];
  isMobile: boolean;
  onSelect: (ticker: string) => void;
  onAddWatch?: (ticker: string) => void;
  addedTickers: Record<string, boolean>;
  pendingTickers: Record<string, boolean>;
}) {
  const showWatch = !!onAddWatch;
  const headers = ["代码", "名称", "现价", "相对强度", "逆市强度", "真实趋势年限", "偏离度历史分位(MA60)", "回调观察"];
  return (
    <div className="card" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: isMobile ? (showWatch ? 1110 : 1010) : (showWatch ? 1190 : 1090), tableLayout: "fixed" }}>
          <colgroup>
            <col style={{ width: 110 }} />
            <col style={{ width: 160 }} />
            <col style={{ width: 100 }} />
            <col style={{ width: 120 }} />
            <col style={{ width: 120 }} />
            <col style={{ width: 130 }} />
            <col style={{ width: 160 }} />
            <col style={{ width: 100 }} />
            {showWatch && <col style={{ width: 100 }} />}
          </colgroup>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              {headers.map((h, i) => (
                <th key={h} style={{ padding: "8px 12px", textAlign: i <= 1 ? "left" : "right", color: "var(--text-muted)", fontWeight: 600, whiteSpace: "nowrap" }}>{h}</th>
              ))}
              {showWatch && (
                <th style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontWeight: 600, whiteSpace: "nowrap" }}>观察</th>
              )}
            </tr>
          </thead>
          <tbody>
            {results.map((r, i) => {
              const added = addedTickers[r.ticker];
              const pending = pendingTickers[r.ticker];
              return (
              <tr
                key={r.ticker}
                onClick={() => onSelect(r.ticker)}
                style={{ borderBottom: i < results.length - 1 ? "1px solid var(--border)" : undefined, cursor: "pointer" }}
              >
                <td style={{ padding: "8px 12px", color: "var(--text)", fontWeight: 600, fontFamily: "var(--font-mono)" }}>{r.ticker}</td>
                <td style={{ padding: "8px 12px", color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={r.name || undefined}>
                  {r.name || "—"}
                </td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {r.latest_price?.toLocaleString("en-US", { maximumFractionDigits: 2 })}
                </td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: pnlColor(r.relative_strength_pct), fontFamily: "var(--font-mono)", fontWeight: 600 }}>
                  {r.relative_strength_pct === null ? "—" : fmtPct(r.relative_strength_pct)}
                </td>
                <td
                  style={{ padding: "8px 12px", textAlign: "right", color: pnlColor(r.counter_trend_strength ?? null), fontFamily: "var(--font-mono)", fontWeight: 600 }}
                  title="逆市抗跌强度：只在大盘下跌日衡量的日超额收益均值。为正=下跌市里还能跑赢大盘（抗跌/逆势），越大势能越强"
                >
                  {r.counter_trend_strength === null || r.counter_trend_strength === undefined ? "—" : fmtPct(r.counter_trend_strength)}
                </td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {r.trend_duration_years ?? (r.trend_duration_days / 250).toFixed(1)}{r.trend_duration_capped ? "+" : ""} 年
                </td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-dim)" }}>
                  {r.deviation_percentile_ma60 === null || r.deviation_percentile_ma60 === undefined
                    ? "—" : `${r.deviation_percentile_ma60.toFixed(0)}%`}
                </td>
                <td style={{ padding: "8px 12px", textAlign: "center" }}>
                  {r.pullback_watch ? (
                    <span
                      title="顺大势逆小势：年线向上、现价跌破 MA60 且仍在年线上方、MA60 偏离度处历史低位（恐慌钟摆）。仅盯盘信号，非买入建议。"
                      style={{
                        display: "inline-block", padding: "1px 8px", borderRadius: 999, fontSize: 11, fontWeight: 600,
                        color: "var(--blue)", background: "color-mix(in srgb, var(--blue) 16%, transparent)",
                        border: "1px solid color-mix(in srgb, var(--blue) 40%, transparent)", whiteSpace: "nowrap",
                      }}
                    >
                      回调观察
                    </span>
                  ) : (
                    <span style={{ color: "var(--text-dim)" }}>—</span>
                  )}
                </td>
                {showWatch && (
                  <td style={{ padding: "8px 12px", textAlign: "right" }}>
                    <button
                      onClick={(e) => { e.stopPropagation(); if (!added && !pending) onAddWatch!(r.ticker); }}
                      disabled={added || pending}
                      className="tag-btn"
                      style={{ fontSize: 11, opacity: added ? 0.6 : 1, cursor: added ? "default" : "pointer" }}
                      title={added ? "已在观察清单" : "加入观察清单"}
                    >
                      {added ? "✓ 已加入" : pending ? "…" : "+ 观察"}
                    </button>
                  </td>
                )}
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ScreenerStatus, type ScreenerResult } from "@/lib/api";
import { fmtPct, pnlColor } from "@/lib/format";
import { useIsMobile } from "@/lib/useIsMobile";

const POLL_INTERVAL_MS = 3000;
// 兜底：轮询这么久还没结束就提示"可能仍在跑"，不强制中断——大股票池扫描本就可能要几分钟。
const POLL_STALL_HINT_MS = 90_000;

/** 选股筛股面板：股票池编辑 + 启动扫描 + 进度轮询 + 结果列表。
 * 嵌入投资总控台「选股」tab；命中的标的点击后交由调用方复用共享的 StockTrendModal。 */
export function StockScreenerPanel({ onSelectTicker }: { onSelectTicker: (ticker: string) => void }) {
  const isMobile = useIsMobile();
  const [universeText, setUniverseText] = useState("");
  const [savingUniverse, setSavingUniverse] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");
  const [status, setStatus] = useState<ScreenerStatus | null>(null);
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

  return (
    <div>
      <p style={{ color: "var(--text-dim)", fontSize: 12, marginBottom: "1rem", lineHeight: 1.7 }}>
        硬性过滤：长周期均线（MA250）必须向上（"大钟摆昂首向上"）。通过后按相对强度（近 60 个交易日跑赢/跑输对应基准指数的幅度）降序展示，
        附趋势持续天数、偏离度历史分位——仅供缩小候选范围，不构成买卖建议，选出来的标的仍需自行做基本面研究。加密货币不纳入筛选（大盘/行业联动等概念对其无对应意义）。
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
          <button onClick={saveUniverse} disabled={savingUniverse} className="tag-btn" style={{ fontSize: 12 }}>
            {savingUniverse ? "保存中…" : "保存股票池"}
          </button>
          <button onClick={startScan} disabled={starting || running} className="tag-btn" style={{ fontSize: 12 }}>
            {running ? "扫描中…" : starting ? "启动中…" : "开始扫描"}
          </button>
          {saveMsg && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{saveMsg}</span>}
          {startMsg && <span style={{ color: "var(--amber)", fontSize: 11 }}>{startMsg}</span>}
        </div>
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
          <SectionTitle>筛选结果（{status?.passed_count ?? 0} / {total}）</SectionTitle>
          {results.length === 0 ? (
            <p style={{ color: "var(--text-dim)", fontSize: 12 }}>无标的通过硬性过滤（年线向上）</p>
          ) : (
            <ResultsTable results={results} isMobile={isMobile} onSelect={onSelectTicker} />
          )}

          {status?.skipped && status.skipped.length > 0 && (
            <p style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 10 }}>
              {status.skipped.length} 只被过滤/跳过：{status.skipped.map((s) => `${s.ticker}(${s.skip_reason})`).join("、")}
            </p>
          )}
        </>
      )}
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

function ResultsTable({ results, isMobile, onSelect }: {
  results: ScreenerResult[]; isMobile: boolean; onSelect: (ticker: string) => void;
}) {
  return (
    <div className="card" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: isMobile ? 720 : 800, tableLayout: "fixed" }}>
          <colgroup>
            <col style={{ width: 110 }} />
            <col style={{ width: 100 }} />
            <col style={{ width: 120 }} />
            <col style={{ width: 140 }} />
            <col style={{ width: 160 }} />
            <col style={{ width: 90 }} />
          </colgroup>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              {["代码", "现价", "相对强度", "趋势持续天数", "偏离度历史分位(MA60)", "标签"].map((h, i) => (
                <th key={h} style={{ padding: "8px 12px", textAlign: i === 0 ? "left" : "right", color: "var(--text-muted)", fontWeight: 600, whiteSpace: "nowrap" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {results.map((r, i) => (
              <tr
                key={r.ticker}
                onClick={() => onSelect(r.ticker)}
                style={{ borderBottom: i < results.length - 1 ? "1px solid var(--border)" : undefined, cursor: "pointer" }}
              >
                <td style={{ padding: "8px 12px", color: "var(--text)", fontWeight: 600, fontFamily: "var(--font-mono)" }}>{r.ticker}</td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {r.latest_price?.toLocaleString("en-US", { maximumFractionDigits: 2 })}
                </td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: pnlColor(r.relative_strength_pct), fontFamily: "var(--font-mono)", fontWeight: 600 }}>
                  {r.relative_strength_pct === null ? "—" : fmtPct(r.relative_strength_pct)}
                </td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {r.trend_duration_days}{r.trend_duration_capped ? "+" : ""} 日
                </td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-dim)" }}>
                  {r.deviation_percentile_ma60 === null || r.deviation_percentile_ma60 === undefined
                    ? "—" : `${r.deviation_percentile_ma60.toFixed(0)}%`}
                </td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text-dim)" }}>{r.tag || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

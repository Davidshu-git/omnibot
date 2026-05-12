import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, type MhxyExecutorStatus, type ProjectOverview, type ProjectRuntimeModels } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { fmt, fmtCost, fmtTime } from "@/lib/format";

const SYNC_FN_MAP: Record<string, () => Promise<{ events_inserted: number }>> = {
  mhxy: api.ingestMhxy,
  "stock-bot": api.ingestStockBot,
  "ehs-bot": api.ingestEhsBot,
};

const PROJECT_ICONS: Record<string, string> = {
  mhxy: "🎮",
  "stock-bot": "📈",
  "ehs-bot": "🛡️",
};

export default function OverviewPage() {
  const [rows, setRows] = useState<ProjectOverview[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [executorStatus, setExecutorStatus] = useState<MhxyExecutorStatus | null>(null);
  const [executorErr, setExecutorErr] = useState("");
  const [runtime, setRuntime] = useState<ProjectRuntimeModels[]>([]);
  const [syncingKey, setSyncingKey] = useState<string | null>(null);
  const [syncMsgs, setSyncMsgs] = useState<Record<string, string>>({});

  const loadRuntime = useCallback(() => {
    return api.runtimeModels().then(setRuntime).catch(() => {});
  }, []);

  const loadExecutorStatus = useCallback(() => {
    api.mhxyExecutorStatus()
      .then((s) => {
        setExecutorStatus(s);
        setExecutorErr("");
      })
      .catch((e) => setExecutorErr(String(e)));
  }, []);

  const load = useCallback(() => {
    api.overview().then(setRows).catch((e) => setErr(String(e)));
    loadRuntime();
    loadExecutorStatus();
  }, [loadExecutorStatus, loadRuntime]);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.overview().then(setRows).catch((e) => setErr(String(e))),
      loadRuntime(),
    ]).finally(() => setLoading(false));
    loadExecutorStatus();
  }, [loadExecutorStatus, loadRuntime]);

  useEffect(() => {
    let executorPollTimer: ReturnType<typeof setInterval> | null = null;
    const runtimePollTimer = setInterval(loadRuntime, 30000);

    const apiOrigin = typeof window !== "undefined"
      ? `${window.location.protocol}//${window.location.hostname}:8000`
      : "http://localhost:8000";
    const es = new EventSource(`${apiOrigin}/api/stream`);

    es.onopen = () => {
      if (executorPollTimer) {
        clearInterval(executorPollTimer);
        executorPollTimer = null;
      }
    };

    es.onmessage = (e) => {
      if (e.data === "executor_status") {
        loadExecutorStatus();
      }
      if (e.data === "model_switched") {
        loadRuntime();
      }
    };

    es.onerror = () => {
      if (!executorPollTimer) {
        executorPollTimer = setInterval(loadExecutorStatus, 30000);
      }
    };

    return () => {
      es.close();
      if (executorPollTimer) clearInterval(executorPollTimer);
      clearInterval(runtimePollTimer);
    };
  }, [loadExecutorStatus, loadRuntime]);

  async function handleSync(projectId: string) {
    const fn = SYNC_FN_MAP[projectId];
    if (!fn) return;
    setSyncingKey(projectId);
    setSyncMsgs((m) => ({ ...m, [projectId]: "" }));
    try {
      const r = await fn();
      setSyncMsgs((m) => ({ ...m, [projectId]: r.events_inserted > 0 ? `+${r.events_inserted}` : "已最新" }));
      load();
    } catch {
      setSyncMsgs((m) => ({ ...m, [projectId]: "失败" }));
    } finally {
      setSyncingKey(null);
    }
  }

  const totalSessions = rows.reduce((s, r) => s + r.total_sessions, 0);
  const totalInput   = rows.reduce((s, r) => s + r.total_input_tokens, 0);
  const totalOutput  = rows.reduce((s, r) => s + r.total_output_tokens, 0);
  const totalTokens  = totalInput + totalOutput;
  const inPct  = totalTokens > 0 ? Math.round(totalInput  / totalTokens * 100) : 0;
  const outPct = 100 - inPct;
  const totalCost = rows.reduce((s, r) => s + (r.total_cost ?? 0), 0);
  const hasCost = rows.some((r) => r.total_cost !== null);

  const runtimeMap = new Map(runtime.map((r) => [r.project_id, r]));

  return (
    <div>
      <div style={{ marginBottom: "1.5rem" }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, color: "var(--text)", marginBottom: 4 }}>全局总览</h1>
        <p style={{ color: "var(--text-muted)", fontSize: 12 }}>
          {rows.length} 个项目 · {totalSessions.toLocaleString()} 次会话
        </p>
      </div>

      {/* Token summary banner */}
      {!loading && totalTokens > 0 && (
        <div className="card" style={{ marginBottom: "1.5rem", display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
            <span style={{ color: "var(--text-muted)", fontSize: 12, fontWeight: 600 }}>Token 消耗总览</span>
            <span style={{ color: "var(--text)", fontWeight: 700, fontSize: 18, fontVariantNumeric: "tabular-nums" }}>
              {fmt(totalTokens)}
              <span style={{ color: "var(--text-dim)", fontWeight: 400, fontSize: 12, marginLeft: 4 }}>tokens</span>
            </span>
          </div>
          <div style={{ display: "flex", height: 6, borderRadius: 3, overflow: "hidden", background: "var(--border)" }}>
            <div style={{ width: `${inPct}%`, background: "var(--blue)", transition: "width 0.5s var(--ease)" }} />
            <div style={{ flex: 1, background: "var(--green)" }} />
          </div>
          <div style={{ display: "flex", gap: "1.5rem", flexWrap: "wrap", alignItems: "center" }}>
            <span style={{ fontSize: 12 }}>
              <span style={{ color: "var(--blue)", marginRight: 4 }}>▪</span>
              <span style={{ color: "var(--text-muted)" }}>输入 </span>
              <span style={{ color: "var(--text)", fontWeight: 600 }}>{fmt(totalInput)}</span>
              <span style={{ color: "var(--text-dim)", fontSize: 11, marginLeft: 4 }}>{inPct}%</span>
            </span>
            <span style={{ fontSize: 12 }}>
              <span style={{ color: "var(--green)", marginRight: 4 }}>▪</span>
              <span style={{ color: "var(--text-muted)" }}>输出 </span>
              <span style={{ color: "var(--text)", fontWeight: 600 }}>{fmt(totalOutput)}</span>
              <span style={{ color: "var(--text-dim)", fontSize: 11, marginLeft: 4 }}>{outPct}%</span>
            </span>
            {hasCost && (
              <span style={{ fontSize: 12, marginLeft: "auto" }}>
                <span style={{ color: "var(--text-muted)" }}>按量计费估算 </span>
                <span style={{ color: "var(--amber)", fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>{fmtCost(totalCost)}</span>
              </span>
            )}
          </div>
        </div>
      )}


      {err && (
        <div style={{
          background: "#1a0a0a", border: "1px solid #3a1a1a", borderRadius: "var(--r)",
          padding: "0.75rem 1rem", color: "var(--red)", fontSize: 12, marginBottom: "1.5rem",
        }}>
          {err}
        </div>
      )}

      <ExecutorStatusCard status={executorStatus} error={executorErr} onRefresh={loadExecutorStatus} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: "1rem" }}>
        {loading
          ? [0, 1, 2].map((i) => <SkeletonCard key={i} />)
          : rows.map((p) => <ProjectCard key={p.project_id} p={p} rt={runtimeMap.get(p.project_id)} syncingKey={syncingKey} syncMsg={syncMsgs[p.project_id]} onSync={handleSync} />)
        }
      </div>
    </div>
  );
}

function ExecutorStatusCard({
  status, error, onRefresh,
}: {
  status: MhxyExecutorStatus | null;
  error: string;
  onRefresh: () => void;
}) {
  const state = error ? "unknown" : (status?.status ?? "unknown");
  const healthy = state === "healthy";
  const unhealthy = state === "unhealthy";
  const stale = state === "stale" || status?.stale;
  const color = healthy ? "var(--green)" : unhealthy ? "var(--red)" : stale ? "var(--amber)" : "var(--text-muted)";
  const bg = healthy ? "rgba(52,211,153,.10)" : unhealthy ? "rgba(248,113,113,.10)" : stale ? "rgba(251,191,36,.10)" : "rgba(139,154,176,.08)";
  const border = healthy ? "rgba(52,211,153,.28)" : unhealthy ? "rgba(248,113,113,.28)" : stale ? "rgba(251,191,36,.28)" : "var(--border)";
  const label = healthy ? "运行正常" : unhealthy ? "异常" : stale ? "状态过期" : "未知";
  const appSummary = summarizeAppHealth(status?.app_health);
  const pid = status?.process?.pid ? String(status.process.pid) : "—";
  const mem = status?.process?.working_set_bytes ? `${Math.round(status.process.working_set_bytes / 1024 / 1024)} MB` : "—";
  const latency = status?.health?.latency_ms !== undefined ? `${status.health.latency_ms} ms` : "—";
  const failures = `${status?.consecutive_failures ?? 0}/${status?.fail_threshold ?? "—"}`;

  return (
    <div className="card" style={{ marginBottom: "1rem", borderColor: border, position: "relative", overflow: "hidden" }}>
      {/* status-tinted gradient overlay — behind all content */}
      <div style={{ position: "absolute", inset: 0, background: `linear-gradient(180deg, ${bg}, transparent 120px)`, pointerEvents: "none" }} />
      <div style={{ position: "relative" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, marginBottom: "1rem" }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ width: 8, height: 8, borderRadius: 999, background: color, boxShadow: healthy ? "0 0 0 4px rgba(52,211,153,.12)" : "none" }} />
              <span style={{ color: "var(--text)", fontWeight: 700, fontSize: 14 }}>Windows Executor</span>
              <span className="badge" style={{ color, background: bg, border: `1px solid ${border}` }}>{label}</span>
              <span className="badge" style={{ color: "var(--text-muted)", background: "var(--border)", border: "1px solid var(--border-hi)", gap: 4 }}>
                <span>🎮</span><span>mhxy</span>
              </span>
            </div>
            <div style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 3, fontFamily: "var(--font-mono)" }}>
              {status?.executor_url ?? "mhxy executor"}
            </div>
          </div>
          <button
            onClick={onRefresh}
            style={{
              padding: "4px 10px",
              borderRadius: "var(--r-sm)",
              border: "1px solid var(--border-hi)",
              background: "transparent",
              color: "var(--blue)",
              fontSize: 11,
              fontWeight: 500,
            }}
          >
            刷新
          </button>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: "0.75rem" }}>
          <Stat label="PID" value={pid} accent={healthy} />
          <Stat label="HTTP 延迟" value={latency} accent={healthy} />
          <Stat label="连续失败" value={failures} accent={!healthy && !stale} />
          <Stat label="内存" value={mem} />
          <Stat label="ADB" value={appSummary.adb} accent={appSummary.adbOk} />
          <Stat label="截图/OCR" value={appSummary.screenshotOcr} accent={appSummary.screenshotOk && appSummary.ocrOk} />
        </div>

        <div style={{ marginTop: "0.85rem", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap", borderTop: "1px solid var(--border)", paddingTop: "0.75rem" }}>
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
            最近检查：{status?.checked_at ? fmtTime(status.checked_at) : "—"}
            {status?.age_sec !== undefined && status.age_sec !== null && (
              <span style={{ marginLeft: 6 }}>({formatAge(status.age_sec)})</span>
            )}
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ color: error || status?.health?.error ? "var(--red)" : "var(--text-dim)", fontSize: 11, maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {error || status?.error || status?.health?.error || status?.last_restart?.reason || "watchdog 状态文件正常"}
            </span>
            <Link href="/executor-instances" style={{ color: "var(--blue)", fontSize: 12, whiteSpace: "nowrap", flexShrink: 0 }}>
              实例详情 →
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}

function summarizeAppHealth(appHealth: MhxyExecutorStatus["app_health"]) {
  const items = appHealth ?? [];
  const total = items.length;
  if (!total) {
    return {
      adb: "—",
      screenshotOcr: "—",
      adbOk: false,
      screenshotOk: false,
      ocrOk: false,
    };
  }

  const adbCount = items.filter((app) => app.adb === true).length;
  const screenshotCount = items.filter((app) => app.screenshot === true).length;
  const ocrCount = items.filter((app) => app.ocr === true).length;

  return {
    adb: `${adbCount}/${total} OK`,
    screenshotOcr: `${screenshotCount}/${total} / ${ocrCount}/${total}`,
    adbOk: adbCount === total,
    screenshotOk: screenshotCount === total,
    ocrOk: ocrCount === total,
  };
}

function ProjectCard({
  p, rt, syncingKey, syncMsg, onSync,
}: {
  p: ProjectOverview;
  rt?: ProjectRuntimeModels;
  syncingKey: string | null;
  syncMsg?: string;
  onSync: (id: string) => void;
}) {
  const icon = PROJECT_ICONS[p.project_id] ?? "◉";
  const canSync = !!SYNC_FN_MAP[p.project_id];
  const syncing = syncingKey === p.project_id;
  const totalTok = p.total_input_tokens + p.total_output_tokens;
  const inPct = totalTok > 0 ? Math.round(p.total_input_tokens / totalTok * 100) : 0;
  const outPct = 100 - inPct;

  return (
    <div className="card" style={{ position: "relative" }}>
      {/* pulse dot when active today */}
      {p.today_sessions > 0 && (
        <span className="pulse-dot" style={{ top: 14, right: 14 }} />
      )}

      {/* header */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: "1.25rem" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 18 }}>{icon}</span>
            <span style={{ color: "var(--text)", fontWeight: 700, fontSize: 14 }}>{p.display_name}</span>
          </div>
          <div style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 2, fontFamily: "var(--font-mono)" }}>
            {p.project_id}
          </div>
        </div>
        {canSync && (
          <button
            onClick={() => onSync(p.project_id)}
            disabled={syncingKey !== null}
            style={{
              padding: "4px 10px",
              borderRadius: "var(--r-sm)",
              border: "1px solid var(--border-hi)",
              background: syncing ? "var(--border)" : "transparent",
              color: syncing ? "var(--text-dim)" : "var(--blue)",
              fontSize: 11,
              fontWeight: 500,
              transition: `all var(--dur) var(--ease)`,
              opacity: syncingKey !== null && !syncing ? 0.45 : 1,
            }}
          >
            {syncing ? "同步中…" : "同步"}
          </button>
        )}
      </div>

      {/* stats */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem", marginBottom: "1rem" }}>
        <Stat label="总会话" value={String(p.total_sessions)} />
        <Stat label="今日调用" value={String(p.today_calls)} accent={p.today_calls > 0} />
        <Stat label="输入 Token" value={fmt(p.total_input_tokens)} />
        <Stat label="输出 Token" value={fmt(p.total_output_tokens)} />
      </div>

      {/* runtime models */}
      {(rt?.text_model || rt?.vl_model) && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: "1rem" }}>
          {rt.text_model && (
            <ModelPill icon="🧠" label={rt.text_model.display_name} sub={rt.text_model.provider} />
          )}
          {rt.vl_model && (
            <ModelPill icon="👁️" label={rt.vl_model.display_name} sub={rt.vl_model.provider} />
          )}
        </div>
      )}

      {/* token bar with tooltip */}
      {totalTok > 0 && (
        <div style={{ marginBottom: "1rem" }}>
          <div
            className="tt"
            style={{ display: "block", width: "100%" }}
          >
            <div style={{
              display: "flex", height: 5, borderRadius: 3, overflow: "hidden",
              background: "var(--border)",
            }}>
              <div style={{
                width: `${inPct}%`, background: "var(--blue)", height: "100%",
                transition: `width 0.5s var(--ease)`,
              }} />
              <div style={{ flex: 1, background: "var(--green)", height: "100%" }} />
            </div>
            <div className="tt-content">
              <span style={{ color: "var(--blue)" }}>↑ 输入 {inPct}%</span>
              {" · "}
              <span style={{ color: "var(--green)" }}>↓ 输出 {outPct}%</span>
              {" · "}
              <span>{fmt(totalTok)} 合计</span>
            </div>
          </div>
          <div style={{ display: "flex", gap: 12, marginTop: 4 }}>
            <span style={{ color: "var(--blue)", fontSize: 10 }}>▪ 输入</span>
            <span style={{ color: "var(--green)", fontSize: 10 }}>▪ 输出</span>
          </div>
        </div>
      )}

      {/* footer */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        borderTop: "1px solid var(--border)", paddingTop: "0.75rem",
      }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
            最近：{fmtTime(p.last_session_at)}
          </span>
          {p.total_cost !== null && (
            <span style={{ fontSize: 11 }}>
              <span style={{ color: "var(--text-dim)" }}>费用估算 </span>
              <span style={{ color: "var(--amber)", fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{fmtCost(p.total_cost)}</span>
            </span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {syncMsg && (
            <span style={{
              color: syncMsg === "失败" ? "var(--red)" : "var(--green)",
              fontSize: 11, fontFamily: "var(--font-mono)",
            }}>
              {syncMsg}
            </span>
          )}
          <Link href={`/sessions?project_id=${p.project_id}`} style={{ color: "var(--blue)", fontSize: 12 }}>
            会话 →
          </Link>
        </div>
      </div>
    </div>
  );
}

function ModelPill({ icon, label, sub }: { icon: string; label: string; sub: string }) {
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: "2px 8px", borderRadius: "var(--r-sm)",
      background: "var(--bg2)", border: "1px solid var(--border-hi)",
      fontSize: 11, lineHeight: "20px",
    }}>
      <span>{icon}</span>
      <span style={{ color: "var(--text)", fontWeight: 600 }}>{label}</span>
      <span style={{ color: "var(--text-dim)" }}>{sub}</span>
    </span>
  );
}

function formatAge(sec: number) {
  if (sec < 60) return `${sec}s 前`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m 前`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m 前`;
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div>
      <div className="stat-label">{label}</div>
      <div style={{
        color: accent ? "var(--amber)" : "var(--text)",
        fontWeight: 700, fontSize: 16,
        fontVariantNumeric: "tabular-nums",
        transition: `color var(--dur) var(--ease)`,
      }}>
        {value}
      </div>
    </div>
  );
}

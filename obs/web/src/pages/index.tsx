import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, type AvailableModelInfo, type MhxyExecutorStatus, type MhxyInstanceDetail, type ProjectOverview, type ProjectRuntimeModels } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { fmt, fmtCost, fmtTime } from "@/lib/format";
import { useIsMobile } from "@/lib/useIsMobile";

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
  const [executorInstances, setExecutorInstances] = useState<MhxyInstanceDetail[]>([]);
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

  const loadExecutorInstances = useCallback(() => {
    api.mhxyExecutorInstances()
      .then((d) => setExecutorInstances(d.instances))
      .catch(() => {});
  }, []);

  const load = useCallback(() => {
    api.overview().then(setRows).catch((e) => setErr(String(e)));
    loadRuntime();
    loadExecutorStatus();
    loadExecutorInstances();
  }, [loadExecutorStatus, loadExecutorInstances, loadRuntime]);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.overview().then(setRows).catch((e) => setErr(String(e))),
      loadRuntime(),
    ]).finally(() => setLoading(false));
    loadExecutorStatus();
    loadExecutorInstances();
  }, [loadExecutorStatus, loadExecutorInstances, loadRuntime]);

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
        loadExecutorInstances();
      }
      if (e.data === "model_switched") {
        loadRuntime();
      }
    };

    es.onerror = () => {
      if (!executorPollTimer) {
        executorPollTimer = setInterval(() => {
          loadExecutorStatus();
          loadExecutorInstances();
        }, 30000);
      }
    };

    return () => {
      es.close();
      if (executorPollTimer) clearInterval(executorPollTimer);
      clearInterval(runtimePollTimer);
    };
  }, [loadExecutorStatus, loadExecutorInstances, loadRuntime]);

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
      <div style={{ marginBottom: "1rem" }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, color: "var(--text)" }}>全局总览</h1>
      </div>

      {/* Token summary banner */}
      {!loading && totalTokens > 0 && (
        <div className="card" style={{
          marginBottom: "1rem",
          padding: "0.85rem 1.1rem",
          display: "flex",
          flexDirection: "column",
          gap: "0.4rem",
        }}>
          <span style={{
            color: "var(--text-muted)",
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.04em",
            textTransform: "uppercase",
          }}>
            Token 消耗总览
          </span>
          <div style={{
            display: "flex",
            alignItems: "baseline",
            gap: "0.85rem",
            flexWrap: "wrap",
            fontVariantNumeric: "tabular-nums",
            fontFeatureSettings: "\"tnum\"",
          }}>
            <span style={{ color: "var(--text)", fontWeight: 700, fontSize: 16 }}>
              {fmt(totalTokens)}
              <span style={{ color: "var(--text-dim)", fontWeight: 400, fontSize: 11, marginLeft: 3 }}>tok</span>
            </span>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              <span style={{ color: "var(--blue)" }}>↑</span> {fmt(totalInput)}
            </span>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              <span style={{ color: "var(--green)" }}>↓</span> {fmt(totalOutput)}
            </span>
            <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{inPct}% 输入 · {outPct}% 输出</span>
            {hasCost && (
              <span style={{ marginLeft: "auto", fontSize: 12 }}>
                <span style={{ color: "var(--text-muted)" }}>费用 </span>
                <span style={{ color: "var(--amber)", fontWeight: 700 }}>{fmtCost(totalCost)}</span>
              </span>
            )}
          </div>
          <div style={{ display: "flex", height: 3, borderRadius: 2, overflow: "hidden", background: "var(--border)" }}>
            <div style={{ width: `${inPct}%`, background: "var(--blue)", transition: "width 0.5s var(--ease)" }} />
            <div style={{ flex: 1, background: "var(--green)" }} />
          </div>
        </div>
      )}


      {err && (
        <div style={{
          background: "#1a0a0a", border: "1px solid #3a1a1a", borderRadius: "var(--r)",
          padding: "0.75rem 1rem", color: "var(--red)", fontSize: 12, marginBottom: "1rem",
        }}>
          {err}
        </div>
      )}

      <ExecutorStatusCard status={executorStatus} error={executorErr} instances={executorInstances} onRefresh={loadExecutorStatus} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: "1rem" }}>
        {loading
          ? [0, 1, 2].map((i) => <SkeletonCard key={i} />)
          : rows.map((p) => <ProjectCard key={p.project_id} p={p} rt={runtimeMap.get(p.project_id)} syncingKey={syncingKey} syncMsg={syncMsgs[p.project_id]} onSync={handleSync} />)
        }
      </div>
    </div>
  );
}

function InstanceGroupsPreview({ instances }: { instances: MhxyInstanceDetail[] }) {
  const groups = new Map<number, MhxyInstanceDetail[]>();
  const standalone: MhxyInstanceDetail[] = [];

  for (const inst of instances) {
    if (inst.group_id !== null && inst.group_id !== undefined) {
      if (!groups.has(inst.group_id)) groups.set(inst.group_id, []);
      groups.get(inst.group_id)!.push(inst);
    } else {
      standalone.push(inst);
    }
  }

  function InstanceChip({ inst }: { inst: MhxyInstanceDetail }) {
    const dotColor = inst.healthy === true ? "var(--green)" : inst.healthy === false ? "var(--red)" : "var(--text-dim)";
    const chipBg = inst.healthy === true ? "rgba(52,211,153,.08)" : inst.healthy === false ? "rgba(248,113,113,.08)" : "rgba(139,154,176,.06)";
    const chipBorder = inst.healthy === true ? "rgba(52,211,153,.22)" : inst.healthy === false ? "rgba(248,113,113,.22)" : "var(--border)";
    const isLeader = inst.role === "leader";
    return (
      <span style={{
        display: "inline-flex", alignItems: "center", gap: 4,
        padding: "2px 8px", borderRadius: 4,
        fontSize: 11, lineHeight: "18px",
        background: chipBg, border: `1px solid ${chipBorder}`,
        color: "var(--text)",
      }}>
        <span style={{ width: 5, height: 5, borderRadius: "50%", background: dotColor, flexShrink: 0 }} />
        <span>{inst.school || inst.port}</span>
        {isLeader && (
          <span style={{ color: "var(--amber)", fontSize: 9, fontWeight: 700, letterSpacing: "0.02em" }}>队长</span>
        )}
      </span>
    );
  }

  const sortedGroups = Array.from(groups.entries()).sort(([a], [b]) => a - b);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
      {sortedGroups.map(([gid, insts]) => {
        const leader = insts.find((i) => i.role === "leader");
        const others = insts.filter((i) => i.role !== "leader");
        const ordered = leader ? [leader, ...others] : insts;
        return (
          <div key={gid} style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
            <span style={{ color: "var(--text-dim)", fontSize: 11, minWidth: 36, flexShrink: 0 }}>
              第{gid + 1}组
            </span>
            {ordered.map((inst) => <InstanceChip key={inst.port} inst={inst} />)}
          </div>
        );
      })}
      {standalone.length > 0 && (
        <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
          <span style={{ color: "var(--text-dim)", fontSize: 11, minWidth: 36, flexShrink: 0 }}>单机</span>
          {standalone.map((inst) => <InstanceChip key={inst.port} inst={inst} />)}
        </div>
      )}
    </div>
  );
}

function ExecutorStatusCard({
  status, error, instances, onRefresh,
}: {
  status: MhxyExecutorStatus | null;
  error: string;
  instances: MhxyInstanceDetail[];
  onRefresh: () => void;
}) {
  const isMobile = useIsMobile();
  const state = error ? "unknown" : (status?.status ?? "unknown");
  const healthy = state === "healthy";
  const unhealthy = state === "unhealthy";
  const stale = state === "stale" || status?.stale;
  const color = healthy ? "var(--green)" : unhealthy ? "var(--red)" : stale ? "var(--amber)" : "var(--text-muted)";
  const bg = healthy ? "rgba(52,211,153,.10)" : unhealthy ? "rgba(248,113,113,.10)" : stale ? "rgba(251,191,36,.10)" : "rgba(139,154,176,.08)";
  const border = healthy ? "rgba(52,211,153,.28)" : unhealthy ? "rgba(248,113,113,.28)" : stale ? "rgba(251,191,36,.28)" : "var(--border)";
  const label = healthy ? "运行正常" : unhealthy ? "异常" : stale ? "状态过期" : "未知";
  const pid = status?.process?.pid ? String(status.process.pid) : "—";
  const mem = status?.process?.working_set_bytes ? `${Math.round(status.process.working_set_bytes / 1024 / 1024)} MB` : "—";
  const latency = status?.health?.latency_ms !== undefined ? `${status.health.latency_ms} ms` : "—";
  const failures = `${status?.consecutive_failures ?? 0}/${status?.fail_threshold ?? "—"}`;

  return (
    <div className="card" style={{ marginBottom: "1rem", borderColor: border, position: "relative", overflow: "hidden" }}>
      <div style={{ position: "absolute", inset: 0, background: `linear-gradient(180deg, ${bg}, transparent 120px)`, pointerEvents: "none" }} />
      <div style={{ position: "relative" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, marginBottom: "1rem" }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", rowGap: 4 }}>
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
              padding: isMobile ? "6px 14px" : "4px 10px",
              borderRadius: "var(--r-sm)",
              border: "1px solid var(--border-hi)",
              background: "transparent",
              color: "var(--blue)",
              fontSize: isMobile ? 12 : 11,
              fontWeight: 500,
            }}
          >
            刷新
          </button>
        </div>

        <div style={{
          display: "flex",
          gap: isMobile ? "0.85rem" : "1.25rem",
          alignItems: "flex-start",
          flexDirection: isMobile ? "column" : "row",
        }}>
          <div style={{
            display: "grid",
            gridTemplateColumns: isMobile ? "1fr 1fr" : "repeat(4, auto)",
            gap: "0.75rem 1.25rem",
            flexShrink: 0,
            width: isMobile ? "100%" : undefined,
          }}>
            <Stat label="PID" value={pid} accent={healthy} />
            <Stat label="HTTP 延迟" value={latency} accent={healthy} />
            <Stat label="连续失败" value={failures} accent={!healthy && !stale} />
            <Stat label="内存" value={mem} />
          </div>
          {instances.length > 0 && (
            <>
              {!isMobile && (
                <div style={{ width: 1, alignSelf: "stretch", background: "var(--border)", flexShrink: 0 }} />
              )}
              <div style={{
                flex: 1,
                minWidth: 0,
                width: isMobile ? "100%" : undefined,
                paddingTop: isMobile ? "0.5rem" : undefined,
                borderTop: isMobile ? "1px solid var(--border)" : undefined,
              }}>
                <InstanceGroupsPreview instances={instances} />
              </div>
            </>
          )}
        </div>

        <div style={{ marginTop: "0.85rem", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap", borderTop: "1px solid var(--border)", paddingTop: "0.75rem" }}>
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
            最近检查：{status?.checked_at ? fmtTime(status.checked_at) : "—"}
            {status?.age_sec !== undefined && status.age_sec !== null && (
              <span style={{ marginLeft: 6 }}>({formatAge(status.age_sec)})</span>
            )}
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{
              color: error || status?.health?.error ? "var(--red)" : "var(--text-dim)",
              fontSize: 11,
              maxWidth: isMobile ? "100%" : 360,
              overflow: "hidden",
              textOverflow: isMobile ? "clip" : "ellipsis",
              whiteSpace: isMobile ? "normal" : "nowrap",
              wordBreak: isMobile ? "break-word" : undefined,
            }}>
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
    <div className="card" style={{ position: "relative", display: "flex", flexDirection: "column", minHeight: 300 }}>
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

      {/* activity meta */}
      <div style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        color: "var(--text-dim)",
        fontSize: 11,
        fontVariantNumeric: "tabular-nums",
        marginBottom: "1rem",
      }}>
        <span>
          <span style={{ color: "var(--text-muted)", fontWeight: 600 }}>{p.total_sessions.toLocaleString()}</span> 会话
        </span>
        <span style={{ width: 3, height: 3, borderRadius: "50%", background: "var(--text-dim)" }} />
        <span>
          今日 <span style={{
            color: p.today_calls > 0 ? "var(--amber)" : "var(--text-muted)",
            fontWeight: 600,
          }}>{p.today_calls.toLocaleString()}</span> 次
        </span>
      </div>

      {/* token bar with tooltip */}
      {totalTok > 0 && (
        <div style={{
          marginBottom: "1rem",
          display: "flex",
          flexDirection: "column",
          gap: "0.35rem",
        }}>
          <div style={{
            display: "flex",
            alignItems: "baseline",
            gap: "0.65rem",
            flexWrap: "wrap",
            fontVariantNumeric: "tabular-nums",
            fontFeatureSettings: "\"tnum\"",
          }}>
            <span style={{ color: "var(--text)", fontWeight: 700, fontSize: 15 }}>
              {fmt(totalTok)}
              <span style={{ color: "var(--text-dim)", fontWeight: 400, fontSize: 10, marginLeft: 3 }}>tok</span>
            </span>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              <span style={{ color: "var(--blue)" }}>↑</span> {fmt(p.total_input_tokens)}
            </span>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              <span style={{ color: "var(--green)" }}>↓</span> {fmt(p.total_output_tokens)}
            </span>
            <span style={{ fontSize: 10, color: "var(--text-dim)" }}>{inPct}% · {outPct}%</span>
          </div>
          <div
            className="tt"
            style={{ display: "block", width: "100%" }}
          >
            <div style={{
              display: "flex", height: 3, borderRadius: 2, overflow: "hidden",
              background: "var(--border)",
            }}>
              <div style={{
                width: `${inPct}%`, background: "var(--blue)",
                transition: `width 0.5s var(--ease)`,
              }} />
              <div style={{ flex: 1, background: "var(--green)" }} />
            </div>
            <div className="tt-content">
              <span style={{ color: "var(--blue)" }}>↑ {inPct}%</span>
              {" · "}
              <span style={{ color: "var(--green)" }}>↓ {outPct}%</span>
              {" · "}
              <span>{fmt(totalTok)} 合计</span>
            </div>
          </div>
        </div>
      )}

      {/* runtime models */}
      {rt && (rt.available_text_models?.length > 0 || rt.available_vl_models?.length > 0) && (
        <div style={{ display: "flex", flexDirection: "column", gap: 5, marginBottom: "0.5rem" }}>
          {rt.available_text_models?.length > 0 && (
            <ModelChipsRow label="主控模型" icon="🧠" models={rt.available_text_models} activeKey={rt.text_model?.model_key ?? null} />
          )}
          {rt.available_vl_models?.length > 0 && (
            <ModelChipsRow label="视觉模型" icon="👁" models={rt.available_vl_models} activeKey={rt.vl_model?.model_key ?? null} />
          )}
        </div>
      )}

      <div style={{ marginTop: "auto" }}>
        {/* footer */}
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          borderTop: "1px solid var(--border)", paddingTop: "0.75rem", minHeight: 45,
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
    </div>
  );
}

function ModelChipsRow({ label, icon, models, activeKey }: {
  label: string;
  icon: string;
  models: AvailableModelInfo[];
  activeKey: string | null;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
      <span style={{ fontSize: 11, lineHeight: 1, flexShrink: 0 }}>{icon}</span>
      <span style={{ color: "var(--text-dim)", fontSize: 10, lineHeight: "18px", flexShrink: 0 }}>{label}</span>
      {models.map((m) => {
        const active = m.key === activeKey;
        return (
          <span key={m.key} style={{
            display: "inline-flex", alignItems: "center", gap: 3,
            padding: "2px 7px", borderRadius: 4,
            fontSize: 10, lineHeight: "16px",
            fontWeight: active ? 700 : 400,
            color: active ? "var(--blue)" : "var(--text-dim)",
            background: active ? "rgba(96,165,250,.1)" : "transparent",
            border: `1px solid ${active ? "rgba(96,165,250,.35)" : "var(--border)"}`,
            transition: "all 0.2s",
          }}>
            {active && (
              <span style={{ width: 4, height: 4, borderRadius: "50%", background: "var(--blue)", flexShrink: 0 }} />
            )}
            {m.display_name}
          </span>
        );
      })}
    </div>
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

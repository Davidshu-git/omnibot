import { useEffect, useState, type CSSProperties } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import type { SessionSummary, NormalizedEvent } from "@/types/events";
import { useIsMobile } from "@/lib/useIsMobile";

// ── helpers ───────────────────────────────────────────────────────────────────

function fmtMsValue(v: unknown): string | null {
  if (typeof v !== "number") return null;
  if (v >= 1000) return `${(v / 1000).toFixed(v >= 10000 ? 1 : 2)}s`;
  return `${Math.round(v)}ms`;
}

function fmtTime(s: string) {
  return new Date(s).toLocaleTimeString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
}

function fmtDateTime(s: string | null | undefined) {
  if (!s) return "—";
  return new Date(s).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
}

const chipStyle = (tone: "ok" | "warn" | "error"): CSSProperties => ({
  display: "inline-block",
  padding: "1px 6px",
  fontSize: 10,
  fontFamily: "var(--font-mono)",
  borderRadius: 3,
  border: "1px solid var(--border)",
  color: { ok: "var(--green)", warn: "var(--amber)", error: "var(--red)" }[tone],
});

function hasObjectContent(v: unknown): boolean {
  return !!v && typeof v === "object" && !Array.isArray(v) && Object.keys(v as Record<string, unknown>).length > 0;
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre style={{
      margin: 0, maxHeight: 180, overflow: "auto",
      color: "var(--text-muted)", fontSize: 10, lineHeight: 1.45,
      fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap",
    }}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

// ── session label ─────────────────────────────────────────────────────────────

function sessionLabel(id: string): string {
  // executor_self_HOSTNAME_YYYYMMDD → HOSTNAME · YYYY-MM-DD
  const m = id.match(/^executor_self_(.+)_(\d{8})$/);
  if (m) {
    const d = m[2];
    return `${m[1]} · ${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
  }
  return id;
}

// ── event row ─────────────────────────────────────────────────────────────────

function EventRow({ event }: { event: NormalizedEvent }) {
  const [expanded, setExpanded] = useState(false);
  const p = event.payload as Record<string, unknown>;
  const subtype = p.type as string | undefined;
  const port = p.port as string | undefined;
  const detail = p.detail as Record<string, unknown> | undefined;

  const icon =
    subtype === "executor_startup"  ? "🚀" :
    subtype === "executor_request"  ? "🪟" :
    subtype === "executor_internal" ? "🔧" : "⚙";

  const canExpand =
    !!p.request_id || !!p.log_file || !!p.events_dir || !!p.adb_path ||
    hasObjectContent(detail) || !!p.error;

  return (
    <div style={{
      padding: "5px 0",
      borderBottom: "1px solid var(--border)",
      fontSize: 12,
    }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        {/* timestamp */}
        <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)", flexShrink: 0 }}>
          {fmtTime(event.timestamp)}
        </span>

        <span>{icon}</span>
        <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)" }}>{subtype}</span>
        {port && <span style={{ color: "var(--teal)", fontFamily: "var(--font-mono)" }}>:{port}</span>}

        {subtype === "executor_startup" && (
          <span style={{ color: "var(--green)" }}>executor 启动</span>
        )}

        {subtype === "executor_request" && <>
          <span style={{ color: "var(--orange)", fontFamily: "var(--font-mono)" }}>
            {p.method as string} {p.path as string}
          </span>
          {typeof p.status_code === "number" && (
            <span style={{
              color: (p.status_code as number) < 400 ? "var(--green)" : "var(--red)",
              fontFamily: "var(--font-mono)",
            }}>
              {p.status_code as number}
            </span>
          )}
          {fmtMsValue(p.duration_ms) && (
            <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{fmtMsValue(p.duration_ms)}</span>
          )}
          {p.slow === true && <span style={chipStyle("warn")}>慢请求</span>}
        </>}

        {subtype === "executor_internal" && <>
          <span style={{ color: "var(--amber)", fontFamily: "var(--font-mono)" }}>{p.op as string}</span>
          {p.success === false && <span style={chipStyle("error")}>✗</span>}
          {p.success === true && <span style={{ color: "var(--green)", fontSize: 11 }}>✓</span>}
          {fmtMsValue(p.duration_ms) && (
            <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{fmtMsValue(p.duration_ms)}</span>
          )}
          {p.error && (
            <span style={{ color: "var(--red)", fontSize: 11 }}>
              {(p.error as string).slice(0, 60)}
            </span>
          )}
        </>}

        {canExpand && (
          <button onClick={() => setExpanded(!expanded)} style={{
            background: "none", border: "none", color: "var(--text-dim)",
            fontSize: 10, cursor: "pointer", padding: 0,
          }}>
            {expanded ? "▲" : "▼"}
          </button>
        )}
      </div>

      {expanded && (
        <div style={{
          marginTop: 6, paddingTop: 6,
          borderTop: "1px solid var(--border)",
          display: "flex", flexDirection: "column", gap: 4,
          color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)",
        }}>
          {p.request_id != null && <span>req: {String(p.request_id)}</span>}
          {!!(p.log_file ?? p.events_dir ?? p.adb_path) && (
            <JsonBlock value={{ log_file: p.log_file, events_dir: p.events_dir, adb_path: p.adb_path }} />
          )}
          {hasObjectContent(detail) && <JsonBlock value={detail} />}
          {p.error != null && <span style={{ color: "var(--red)" }}>{String(p.error)}</span>}
        </div>
      )}
    </div>
  );
}

// ── page ──────────────────────────────────────────────────────────────────────

export default function ExecutorLogsPage() {
  const isMobile = useIsMobile();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<NormalizedEvent[]>([]);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingTimeline, setLoadingTimeline] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.sessions({ project_id: "mhxy", agent_id: "windows-executor", limit: 30 })
      .then((list) => {
        setSessions(list);
        if (list.length > 0) setSelectedId(list[0].id);
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoadingSessions(false));
  }, []);

  useEffect(() => {
    if (!selectedId) return;
    setLoadingTimeline(true);
    setEvents([]);
    api.timeline(selectedId)
      .then((r) => setEvents(r.events))
      .catch((e) => setErr(String(e)))
      .finally(() => setLoadingTimeline(false));
  }, [selectedId]);

  const selected = sessions.find((s) => s.id === selectedId);

  return (
    <div style={{ height: "calc(100vh - 3.5rem)", display: "flex", flexDirection: "column" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "1rem", flexWrap: "wrap" }}>
        <Link href="/" style={{ color: "var(--text-muted)", fontSize: 12 }}>← 总览</Link>
        <h1 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)" }}>Executor 执行日志</h1>
        <span className="badge" style={{ color: "var(--text-muted)" }}>windows-executor</span>
        {err && <span style={{ color: "var(--red)", fontSize: 12 }}>{err}</span>}
      </div>

      <div style={{
        flex: 1, display: "flex", gap: "1rem", minHeight: 0,
        flexDirection: isMobile ? "column" : "row",
      }}>
        {/* left: session list */}
        {(!isMobile || !selectedId) && (
          <div style={{
            width: isMobile ? "100%" : 220,
            flexShrink: 0,
            display: "flex",
            flexDirection: "column",
            borderRight: isMobile ? "none" : "1px solid var(--border)",
            paddingRight: isMobile ? 0 : "1rem",
            minHeight: 0,
          }}>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: "0.5rem" }}>
              日期会话（最近 30 条）
            </div>
            <div style={{ flex: 1, overflowY: "auto" }}>
              {loadingSessions ? (
                <div style={{ color: "var(--text-dim)", fontSize: 12 }}>加载中…</div>
              ) : sessions.length === 0 ? (
                <div style={{ color: "var(--text-muted)", fontSize: 12 }}>
                  暂无日志。executor 需先上报事件并完成摄取。
                </div>
              ) : sessions.map((s) => (
                <div
                  key={s.id}
                  onClick={() => setSelectedId(s.id)}
                  style={{
                    padding: "8px 10px",
                    borderRadius: "var(--r-sm)",
                    cursor: "pointer",
                    borderLeft: `3px solid ${selectedId === s.id ? "var(--blue)" : "transparent"}`,
                    background: selectedId === s.id ? "rgba(96,165,250,.08)" : "transparent",
                    marginBottom: 2,
                  }}
                >
                  <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text)" }}>
                    {sessionLabel(s.id)}
                  </div>
                  {(s.ended_at ?? s.started_at) && (
                    <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 2 }}>
                      {fmtDateTime(s.ended_at ?? s.started_at)}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* right: timeline */}
        {(!isMobile || !!selectedId) && (
          <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
            {isMobile && selectedId && (
              <button onClick={() => setSelectedId(null)} style={{
                background: "none", border: "none", color: "var(--blue)",
                fontSize: 12, cursor: "pointer", padding: "0 0 0.5rem", textAlign: "left",
              }}>
                ← 返回列表
              </button>
            )}

            {selected && (
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: "0.5rem" }}>
                {sessionLabel(selected.id)}
                {selected.started_at && (
                  <span style={{ marginLeft: 8 }}>{fmtDateTime(selected.started_at)}</span>
                )}
              </div>
            )}

            <div style={{ flex: 1, overflowY: "auto" }}>
              {loadingTimeline ? (
                <div style={{ color: "var(--text-dim)", fontSize: 12, padding: "1rem 0" }}>加载中…</div>
              ) : events.length === 0 && !loadingTimeline ? (
                <div style={{ color: "var(--text-muted)", fontSize: 12, padding: "1rem 0" }}>
                  {selectedId ? "该会话暂无事件" : "请选择一个会话"}
                </div>
              ) : events.map((e) => (
                <EventRow key={e.event_id} event={e} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

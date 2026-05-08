import { useEffect, useState } from "react";
import { useRouter } from "next/router";
import Link from "next/link";
import { api } from "@/lib/api";
import type { NormalizedEvent } from "@/types/events";
import CopyableId from "@/components/CopyableId";
import { fmtCost } from "@/lib/format";

const EVENT_COLORS: Record<string, string> = {
  session_started: "var(--green)",
  session_ended:   "var(--red)",
  message:         "var(--blue)",
  thought:         "var(--purple)",
  model_call:      "var(--amber)",
  tool_call:       "var(--orange)",
  tool_result:     "var(--orange)",
  metric:          "var(--teal)",
  event:           "var(--teal)",
  error:           "var(--red)",
};

function fmtTime(s: string) {
  return new Date(s).toLocaleTimeString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
}

function fmt(n: number) {
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function PayloadView({ event }: { event: NormalizedEvent }) {
  const [expanded, setExpanded] = useState(false);
  const p = event.payload as Record<string, unknown>;
  const t = event.event_type;

  if (t === "message") {
    const content = p.content as string;
    return (
      <div>
        <span style={{ color: "var(--blue)", fontSize: 11, marginRight: 8, fontFamily: "var(--font-mono)" }}>
          [{p.role as string}]
        </span>
        <span style={{ color: "var(--text)", whiteSpace: "pre-wrap", fontSize: 12 }}>{content}</span>
      </div>
    );
  }
  if (t === "thought") {
    const content = p.content as string;
    const truncated = !expanded && content?.length > 200;
    return (
      <div>
        <div style={{ color: "var(--purple)", opacity: 0.6, fontSize: 10, marginBottom: 3 }}>
          💭 {p.kind as string}
        </div>
        <div style={{ color: "var(--purple)", whiteSpace: "pre-wrap", fontSize: 12, opacity: 0.85 }}>
          {truncated ? content.slice(0, 200) + "…" : content}
        </div>
        {content?.length > 200 && (
          <button onClick={() => setExpanded(!expanded)} style={{
            background: "none", border: "none", color: "var(--purple)", fontSize: 10,
            cursor: "pointer", padding: 0, marginTop: 3,
          }}>
            {expanded ? "收起" : "展开"}
          </button>
        )}
      </div>
    );
  }
  if (t === "model_call") {
    const durMs = p.duration_ms as number | null;
    const hasDetail = p.prompt != null || p.raw_output != null;
    return (
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ color: "var(--amber)", fontWeight: 700, fontFamily: "var(--font-mono)", fontSize: 12 }}>
            {p.model as string}
          </span>
          <span style={{ color: "var(--text-dim)", fontFamily: "var(--font-mono)", fontSize: 11 }}>
            ↑{p.input_tokens as number} ↓{p.output_tokens as number}
            {durMs != null && ` ${Math.round(durMs)}ms`}
          </span>
          {event.run_id && (
            <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)" }}>
              run:{event.run_id.slice(-8)}
            </span>
          )}
          {hasDetail && (
            <button onClick={() => setExpanded(!expanded)} style={{
              background: "none", border: "none", color: "var(--text-dim)",
              fontSize: 10, cursor: "pointer", padding: 0,
            }}>
              {expanded ? "▲" : "▼ 详情"}
            </button>
          )}
        </div>
        {expanded && hasDetail && (
          <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 4 }}>
            {p.prompt != null && (
              <pre style={{
                padding: "6px 10px", background: "rgba(0,0,0,.4)",
                borderRadius: "var(--r-sm)", color: "var(--text-muted)",
                fontSize: 10, overflow: "auto", maxHeight: 160, whiteSpace: "pre-wrap",
              }}>
                {String(p.prompt)}
              </pre>
            )}
            {p.raw_output != null && (
              <pre style={{
                padding: "6px 10px", background: "rgba(0,0,0,.4)",
                borderRadius: "var(--r-sm)", color: "var(--text-muted)",
                fontSize: 10, overflow: "auto", maxHeight: 160, whiteSpace: "pre-wrap",
              }}>
                {String(p.raw_output)}
              </pre>
            )}
          </div>
        )}
      </div>
    );
  }
  if (t === "tool_call") {
    return (
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "var(--orange)", fontWeight: 700, fontFamily: "var(--font-mono)", fontSize: 12 }}>
            ⚙ {p.tool_name as string}
          </span>
          {p.arguments != null && (
            <button onClick={() => setExpanded(!expanded)} style={{
              background: "none", border: "none", color: "var(--text-dim)",
              fontSize: 10, cursor: "pointer", padding: 0,
            }}>
              {expanded ? "▲" : "▼ 参数"}
            </button>
          )}
        </div>
        {expanded && p.arguments != null && (
          <pre style={{
            marginTop: 4, padding: "6px 10px",
            background: "rgba(0,0,0,.4)", borderRadius: "var(--r-sm)",
            color: "var(--text-muted)", fontSize: 10, overflow: "auto", maxHeight: 120,
          }}>
            {JSON.stringify(p.arguments, null, 2)}
          </pre>
        )}
      </div>
    );
  }
  if (t === "tool_result") {
    const ok = p.success as boolean;
    const durMs = p.duration_ms as number | null;
    const result = typeof p.result === "string" ? p.result : JSON.stringify(p.result);
    const truncated = !expanded && (result?.length ?? 0) > 200;
    return (
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: ok ? "var(--green)" : "var(--red)", fontFamily: "var(--font-mono)", fontSize: 12 }}>
            {ok ? "✓" : "✗"} {p.tool_name as string}
          </span>
          {durMs != null && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{Math.round(durMs)}ms</span>}
          {(result?.length ?? 0) > 200 && (
            <button onClick={() => setExpanded(!expanded)} style={{
              background: "none", border: "none", color: "var(--text-dim)", fontSize: 10, cursor: "pointer", padding: 0,
            }}>
              {expanded ? "收起" : "展开"}
            </button>
          )}
        </div>
        {result && (
          <div style={{ color: "var(--text-muted)", fontSize: 11, marginTop: 3, whiteSpace: "pre-wrap", fontFamily: "var(--font-mono)" }}>
            {truncated ? result.slice(0, 200) + "…" : result}
          </div>
        )}
      </div>
    );
  }
  if (t === "error") {
    return (
      <div style={{ color: "var(--red)", fontSize: 12 }}>
        <strong>{p.name as string}: </strong>{p.message as string}
      </div>
    );
  }
  if (t === "task_event") {
    const subtype = p.type as string;
    const port = p.port as string;
    const state = p.state as string;
    const elapsed = typeof p.elapsed_ms === "number" || typeof p.duration_ms === "number"
      ? `${Math.round((p.elapsed_ms ?? p.duration_ms) as number)}ms` : null;
    const stateColor = ["disconnected", "failed"].includes(state) ? "var(--red)"
      : state === "main_ui" ? "var(--green)" : state === "login_screen" ? "var(--amber)" : "var(--text-dim)";

    if (subtype === "executor_perf") {
      const timing = p.timing as Record<string, number> | undefined;
      return (
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
          <span>⏱</span>
          <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)" }}>executor_perf</span>
          {port && <span style={{ color: "var(--teal)", fontFamily: "var(--font-mono)" }}>:{port}</span>}
          {timing?.total_s != null && (
            <span style={{ color: "var(--teal)", fontSize: 11 }}>{timing.total_s.toFixed(2)}s</span>
          )}
          {typeof p.text_count === "number" && (
            <span style={{ color: "var(--text-dim)", fontSize: 10 }}>{p.text_count} texts</span>
          )}
          {timing && (
            <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)" }}>
              截图{timing.screenshot_s?.toFixed(2)}s OCR{timing.ocr_s?.toFixed(2)}s
            </span>
          )}
        </div>
      );
    }

    if (subtype === "executor_request") {
      return (
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
          <span>🪟</span>
          <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)" }}>executor_request</span>
          {port && <span style={{ color: "var(--teal)", fontFamily: "var(--font-mono)" }}>:{port}</span>}
          <span style={{ color: "var(--orange)", fontFamily: "var(--font-mono)" }}>{p.method as string} {p.path as string}</span>
          {typeof p.status_code === "number" && (
            <span style={{ color: (p.status_code as number) < 400 ? "var(--green)" : "var(--red)", fontFamily: "var(--font-mono)" }}>
              {p.status_code as number}
            </span>
          )}
          {elapsed && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{elapsed}</span>}
        </div>
      );
    }

    if (subtype === "executor_startup") {
      return (
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
          <span>🚀</span>
          <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)" }}>executor_startup</span>
          <span style={{ color: "var(--green)" }}>executor 启动</span>
          {p.host && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{p.host as string}</span>}
        </div>
      );
    }

    return (
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
        <span>{subtype === "instance_status" ? "📊" : subtype === "reconnect_result" ? "🔌" :
               subtype === "executor_perf" ? "⏱" :
               subtype === "executor_internal" ? "🔧" :
               subtype?.startsWith("task_started") ? "🎮" : subtype?.startsWith("task_completed") ? "✅" :
               subtype?.startsWith("task_failed") ? "❌" : subtype?.startsWith("task_step") ? "▶" :
               subtype === "scan_started" ? "🔍" : "⚙"}</span>
        <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)" }}>{subtype}</span>
        {port && <span style={{ color: "var(--teal)", fontFamily: "var(--font-mono)" }}>:{port}</span>}
        {state && <span style={{ color: stateColor, fontFamily: "var(--font-mono)", fontSize: 11 }}>{state}</span>}
        {p.task_name && <span style={{ color: "var(--text-dim)" }}>{p.task_name as string}</span>}
        {elapsed && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{elapsed}</span>}
        {p.message && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{(p.message as string).slice(0, 80)}</span>}
      </div>
    );
  }
  return (
    <pre style={{ color: "var(--text-muted)", fontSize: 10, overflow: "auto", maxHeight: 80, margin: 0 }}>
      {JSON.stringify(p, null, 2)}
    </pre>
  );
}

export default function TraceDetailPage() {
  const router = useRouter();
  const traceId = router.query.trace_id as string;
  const [events, setEvents] = useState<NormalizedEvent[]>([]);
  const [totalCost, setTotalCost] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!traceId) return;
    setLoading(true);
    api.trace(traceId)
      .then((r) => { setEvents(r.events); setTotalCost(r.total_cost ?? null); })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [traceId]);

  const sessionId = events[0]?.session_id;
  const projectId = events[0]?.project_id;

  const modelCalls = events.filter((e) => e.event_type === "model_call");
  const toolCalls  = events.filter((e) => e.event_type === "tool_call");
  const inTok  = modelCalls.reduce((s, e) => s + (((e.payload as Record<string,unknown>).input_tokens as number) || 0), 0);
  const outTok = modelCalls.reduce((s, e) => s + (((e.payload as Record<string,unknown>).output_tokens as number) || 0), 0);
  const firstTs = events[0]?.timestamp;
  const lastTs  = events[events.length - 1]?.timestamp;
  const durMs = firstTs && lastTs
    ? new Date(lastTs).getTime() - new Date(firstTs).getTime()
    : null;

  return (
    <div>
      {/* breadcrumb */}
      <div className="breadcrumb" style={{ marginBottom: "1.25rem" }}>
        <Link href="/">总览</Link>
        <span className="breadcrumb-sep">›</span>
        {sessionId && (
          <>
            <Link href={`/sessions?session_id=${sessionId}${projectId ? `&project_id=${projectId}` : ""}#trace-${traceId}`}>
              会话
            </Link>
            <span className="breadcrumb-sep">›</span>
          </>
        )}
        <span className="breadcrumb-current">Trace 详情</span>
        <CopyableId id={traceId ?? ""} truncate={32} />
      </div>

      {err && <p style={{ color: "var(--red)", marginBottom: "1rem" }}>{err}</p>}
      {loading && <p style={{ color: "var(--text-dim)" }}>加载中…</p>}

      {/* summary cards */}
      {events.length > 0 && (
        <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", marginBottom: "1.5rem" }}>
          <SummaryCard label="总事件" value={String(events.length)} color="var(--text)" />
          {durMs != null && (
            <SummaryCard label="总耗时" value={`${(durMs / 1000).toFixed(2)}s`} color="var(--teal)" />
          )}
          <SummaryCard label="模型调用" value={String(modelCalls.length)} color="var(--amber)" />
          <SummaryCard label="工具调用" value={String(toolCalls.length)} color="var(--orange)" />
          <SummaryCard label="输入 Token" value={fmt(inTok)} color="var(--blue)" />
          <SummaryCard label="输出 Token" value={fmt(outTok)} color="var(--green)" />
          <SummaryCard label="费用" value={totalCost != null ? fmtCost(totalCost) : "包月"} color="var(--teal)" />
        </div>
      )}

      {/* event list */}
      <div className="trace-timeline">
        {events.map((e) => {
          const color = EVENT_COLORS[e.event_type] ?? "var(--text-dim)";
          const isError = e.event_type === "error";
          return (
            <div
              key={e.event_id}
              className={isError ? "event-error" : ""}
              style={{
                display: "flex",
                gap: "0.75rem",
                marginBottom: "0.6rem",
                position: "relative",
              }}
            >
              <div
                className="trace-dot"
                style={{ background: color }}
              />
              <div style={{ color: "var(--text-dim)", fontSize: 10, width: 56, flexShrink: 0, paddingTop: 2, fontFamily: "var(--font-mono)" }}>
                {fmtTime(e.timestamp)}
              </div>
              <div style={{ color, fontSize: 10, width: 84, flexShrink: 0, paddingTop: 2, fontFamily: "var(--font-mono)" }}>
                {e.event_type}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <PayloadView event={e} />
              </div>
            </div>
          );
        })}
      </div>

      {!loading && events.length === 0 && !err && (
        <p style={{ color: "var(--text-dim)" }}>Trace 暂无事件</p>
      )}
    </div>
  );
}

function SummaryCard({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div style={{
      background: "var(--card)",
      border: "1px solid var(--border)",
      borderRadius: "var(--r)",
      padding: "10px 16px",
      minWidth: 100,
    }}>
      <div className="stat-label">{label}</div>
      <div style={{ color, fontWeight: 700, fontSize: 18, fontVariantNumeric: "tabular-nums", marginTop: 2 }}>
        {value}
      </div>
    </div>
  );
}

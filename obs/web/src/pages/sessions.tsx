import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { useRouter } from "next/router";
import Link from "next/link";
import { marked } from "marked";
import DOMPurify from "isomorphic-dompurify";
import { api } from "@/lib/api";
import type { SessionSummary, NormalizedEvent } from "@/types/events";
import CopyableId from "@/components/CopyableId";
import MhxyLiveStreamPanel from "@/components/MhxyLiveStreamPanel";
import { SkeletonSessionItem } from "@/components/Skeleton";
import { useIsMobile } from "@/lib/useIsMobile";
import { sseOrigin } from "@/lib/gateway";

marked.use({ breaks: true, gfm: true });

// ── colors ────────────────────────────────────────────────────────────────────

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
  task_event:      "var(--green)",
  error:           "var(--red)",
};

const TASK_EVENT_ICONS: Record<string, string> = {
  task_started:           "🎮",
  task_completed:         "✅",
  task_failed:            "❌",
  task_needs_human:       "👨‍🔧",
  task_step_started:      "▶",
  task_step_completed:    "✓",
  task_step_failed:       "✗",
  task_denylist_triggered:"🚨",
  instance_status:        "📊",
  reconnect_step:         "🔄",
  reconnect_result:       "🔌",
  executor_perf:          "⏱",
  executor_request:       "🪟",
  executor_internal:      "🔧",
  executor_startup:       "🚀",
};

const AGENT_DISPLAY: Record<string, string> = {
  "mhxy-bot":  "梦幻西游 Bot",
  "stock-bot": "OmniStock 量化助理",
  "ehs-bot":   "OmniEHS 安全合规助理",
  "mhxy":      "梦幻西游 Bot",
  "stock":     "OmniStock 量化助理",
  "ehs":       "OmniEHS 安全合规助理",
};

const AGENT_PALETTE = ["var(--cat-1)", "var(--cat-2)", "var(--cat-3)"];
const CHAT_PROJECTS = new Set(["stock-bot", "ehs-bot", "mhxy-bot"]);
const _colorCache: Record<string, string> = {};
let _colorIdx = 0;
function agentColor(key: string): string {
  if (!_colorCache[key]) _colorCache[key] = AGENT_PALETTE[_colorIdx++ % AGENT_PALETTE.length];
  return _colorCache[key];
}

// ── helpers ───────────────────────────────────────────────────────────────────

function shortId(id: string): string {
  const parts = id.split("_");
  if (parts.length >= 4) return parts.slice(2, -1).join("_");
  return id.length > 22 ? id.slice(0, 10) + "…" + id.slice(-6) : id;
}
function fmtTime(s: string) {
  return new Date(s).toLocaleTimeString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
}
function fmtDate(s: string | null | undefined) {
  if (!s) return "—";
  return new Date(s).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
}
function parseMd(text: string): string {
  return DOMPurify.sanitize(marked.parse(text) as string);
}
function fmtMsValue(v: unknown): string | null {
  if (typeof v !== "number") return null;
  if (v >= 1000) return `${(v / 1000).toFixed(v >= 10000 ? 1 : 2)}s`;
  return `${Math.round(v)}ms`;
}
function todayYmdInShanghai(): string {
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const y = parts.find((p) => p.type === "year")?.value ?? "";
  const m = parts.find((p) => p.type === "month")?.value ?? "";
  const d = parts.find((p) => p.type === "day")?.value ?? "";
  return `${y}${m}${d}`;
}
function parseBotSessionId(sessionId: string): { userId: number; ymd: string } | null {
  const m = /^tg_session_(.+)_(\d+)_(\d{8})$/.exec(sessionId);
  if (!m) return null;
  return { userId: Number(m[2]), ymd: m[3] };
}
function chatErrorMessage(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  if (msg.startsWith("401")) return "共享密钥校验失败，请检查 OBS_BOT_CHAT_TOKEN。";
  if (msg.startsWith("403")) return "该 user_id 不在 bot 白名单内。";
  if (msg.startsWith("404")) return "当前项目没有可用的 bot 对话入口。";
  if (msg.startsWith("502")) return "obs-api 无法连接 bot 对话服务，请检查容器端口和 URL。";
  if (msg.startsWith("504")) return "模型推理超时，请稍后重试。";
  return msg;
}
const chipStyle = (tone: "ok" | "warn" | "error" | "muted"): CSSProperties => ({
  display: "inline-block",
  padding: "1px 6px",
  fontSize: 10,
  fontFamily: "var(--font-mono)",
  borderRadius: 3,
  border: "1px solid var(--border)",
  color: {
    ok: "var(--green)",
    warn: "var(--amber)",
    error: "var(--red)",
    muted: "var(--text-muted)",
  }[tone],
});
function hasObjectContent(v: unknown): boolean {
  return !!v && typeof v === "object" && !Array.isArray(v) && Object.keys(v as Record<string, unknown>).length > 0;
}
function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre style={{
      margin: 0,
      maxHeight: 180,
      overflow: "auto",
      color: "var(--text-muted)",
      fontSize: 10,
      lineHeight: 1.45,
      fontFamily: "var(--font-mono)",
      whiteSpace: "pre-wrap",
    }}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

// ── event detail ──────────────────────────────────────────────────────────────

function EventDetail({ event }: { event: NormalizedEvent }) {
  const p = event.payload as Record<string, unknown>;
  const t = event.event_type;
  const initiallyExpanded = t === "task_event" && p.type === "executor_perf";
  const [expanded, setExpanded] = useState(initiallyExpanded);

  if (t === "message") {
    const role = p.role as string;
    const content = (p.content as string) || "";
    const isUser = role === "user";
    const isSystem = role === "system";
    const avatarClass = isUser ? "avatar-user" : isSystem ? "avatar-system" : "avatar-assistant";
    const avatarLabel = isUser ? "U" : isSystem ? "S" : "A";
    const bgColor = isUser
      ? "rgba(96,165,250,.07)"
      : isSystem
      ? "rgba(251,191,36,.05)"
      : "rgba(52,211,153,.05)";
    const borderColor = isUser
      ? "rgba(96,165,250,.15)"
      : isSystem
      ? "rgba(251,191,36,.12)"
      : "rgba(52,211,153,.1)";
    const truncated = !expanded && content.length > 400;
    const html = parseMd(truncated ? content.slice(0, 400) + "…" : content);

    return (
      <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
        <div className={`avatar ${avatarClass}`}>{avatarLabel}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{
            background: bgColor,
            border: `1px solid ${borderColor}`,
            borderRadius: "var(--r)",
            padding: "8px 12px",
            transition: `background var(--dur) var(--ease)`,
          }}>
            <div
              className="md-content"
              dangerouslySetInnerHTML={{ __html: html }}
            />
          </div>
          {content.length > 400 && (
            <button onClick={() => setExpanded(!expanded)} style={{
              background: "none", border: "none", color: "var(--blue)", fontSize: 11,
              cursor: "pointer", marginTop: 4, padding: 0,
              transition: `opacity var(--dur) var(--ease)`,
            }}>
              {expanded ? "收起 ▲" : "展开全文 ▼"}
            </button>
          )}
        </div>
      </div>
    );
  }

  if (t === "thought") {
    const content = (p.content as string) || "";
    const truncated = !expanded && content.length > 250;
    const preview = truncated ? content.slice(0, 250) + "…" : content;
    return (
      <div style={{
        background: "rgba(196,181,253,.05)",
        border: "1px solid rgba(196,181,253,.12)",
        borderRadius: "var(--r)",
        padding: "8px 12px",
      }}>
        <div style={{ color: "rgba(196,181,253,.45)", fontSize: 10, marginBottom: 4 }}>
          💭 思考 · {p.provider as string}
        </div>
        <div style={{ color: "var(--purple)", fontSize: 12, lineHeight: 1.65, whiteSpace: "pre-wrap", opacity: 0.8 }}>
          {preview}
        </div>
        {content.length > 250 && (
          <button onClick={() => setExpanded(!expanded)} style={{
            background: "none", border: "none", color: "var(--purple)", fontSize: 11,
            cursor: "pointer", marginTop: 4, padding: 0,
          }}>
            {expanded ? "收起 ▲" : "展开 ▼"}
          </button>
        )}
      </div>
    );
  }

  if (t === "model_call") {
    const durMs = p.duration_ms as number | null;
    const ok = p.success as boolean;
    const prompt = p.prompt as string | null;
    const rawOutput = p.raw_output as string | null;
    const hasDetail = !!(prompt || rawOutput);
    return (
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ color: "var(--amber)", fontFamily: "var(--font-mono)", fontSize: 12, fontWeight: 600 }}>
            {p.model as string}
          </span>
          <span style={{ color: "var(--text-dim)", fontSize: 11, fontFamily: "var(--font-mono)" }}>
            ↑{(p.input_tokens as number) || 0} ↓{(p.output_tokens as number) || 0}
            {durMs != null && ` ${Math.round(durMs)}ms`}
          </span>
          {!ok && <span style={{ color: "var(--red)", fontSize: 11, fontWeight: 600 }}>FAILED</span>}
          {hasDetail && (
            <button onClick={() => setExpanded(!expanded)} style={{
              background: "none", border: "none", color: "var(--text-dim)",
              fontSize: 10, cursor: "pointer", padding: 0,
            }}>
              {expanded ? "▲ 隐藏" : "▼ 详情"}
            </button>
          )}
        </div>
        {expanded && hasDetail && (
          <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 4 }}>
            {prompt && (
              <pre style={{
                margin: 0, padding: "6px 10px",
                background: "rgba(0,0,0,.4)", border: "1px solid var(--border)",
                borderRadius: "var(--r-sm)",
                color: "var(--text-muted)", fontSize: 11, whiteSpace: "pre-wrap",
                wordBreak: "break-all", maxHeight: 180, overflow: "auto",
              }}>
                <span style={{ color: "var(--text-dim)", fontSize: 10 }}>prompt↓</span>{"\n"}{prompt}
              </pre>
            )}
            {rawOutput && (
              <pre style={{
                margin: 0, padding: "6px 10px",
                background: "rgba(0,0,0,.4)", border: "1px solid var(--border)",
                borderRadius: "var(--r-sm)",
                color: "var(--green)", fontSize: 11, whiteSpace: "pre-wrap",
                wordBreak: "break-all", maxHeight: 180, overflow: "auto",
              }}>
                <span style={{ color: "var(--text-dim)", fontSize: 10 }}>output↓</span>{"\n"}{rawOutput}
              </pre>
            )}
          </div>
        )}
      </div>
    );
  }

  if (t === "tool_call") {
    const args = p.arguments;
    return (
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "var(--orange)", fontWeight: 700, fontSize: 12, fontFamily: "var(--font-mono)" }}>
            ⚙ {p.tool_name as string}
          </span>
          {args != null && (
            <button onClick={() => setExpanded(!expanded)} style={{
              background: "none", border: "none", color: "var(--text-dim)",
              fontSize: 10, cursor: "pointer", padding: 0,
            }}>
              {expanded ? "▲ 隐藏" : "▼ 参数"}
            </button>
          )}
        </div>
        {expanded && args != null && (
          <pre style={{
            marginTop: 6, padding: "6px 10px",
            background: "rgba(0,0,0,.4)", border: "1px solid var(--border)",
            borderRadius: "var(--r-sm)",
            color: "var(--text-muted)", fontSize: 11, overflow: "auto", maxHeight: 180,
          }}>
            {JSON.stringify(args, null, 2)}
          </pre>
        )}
      </div>
    );
  }

  if (t === "tool_result") {
    const ok = p.success as boolean;
    const durMs = p.duration_ms as number | null;
    const result = typeof p.result === "string" ? p.result : JSON.stringify(p.result);
    const meta = p.meta as Record<string, unknown> | undefined;
    const steps = Array.isArray(meta?.steps) ? meta.steps : [];
    const truncated = !expanded && (result?.length ?? 0) > 250;
    const preview = truncated ? result!.slice(0, 250) + "…" : result;
    return (
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: ok ? "var(--green)" : "var(--red)", fontFamily: "var(--font-mono)", fontSize: 12 }}>
            {ok ? "✓" : "✗"} {p.tool_name as string}
          </span>
          {durMs != null && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{Math.round(durMs)}ms</span>}
          {(result?.length ?? 0) > 250 && (
            <button onClick={() => setExpanded(!expanded)} style={{
              background: "none", border: "none", color: "var(--text-dim)",
              fontSize: 10, cursor: "pointer", padding: 0,
            }}>
              {expanded ? "收起" : "展开"}
            </button>
          )}
        </div>
        {result && (
          <div style={{ marginTop: 4, color: "var(--text-muted)", fontSize: 11, whiteSpace: "pre-wrap", fontFamily: "var(--font-mono)", lineHeight: 1.5 }}>
            {preview}
          </div>
        )}
        {meta?.kind === "instance_diagnosis" && (
          <div style={{ marginTop: 6 }}>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <span style={chipStyle("muted")}>端口 {String(meta.port ?? "")}</span>
              <span style={chipStyle(meta.code === "unknown_ok" ? "ok" : "warn")}>
                {String(meta.code ?? "")}
              </span>
              <span style={chipStyle("muted")}>{String(meta.state ?? "")}</span>
              {meta.needs_human === true && <span style={chipStyle("error")}>需人工</span>}
            </div>
            {steps.length > 0 && (
              <ol style={{ marginTop: 6, paddingLeft: 18, color: "var(--text-muted)", fontSize: 11 }}>
                {steps.map((s, i) => <li key={i}>{String(s)}</li>)}
              </ol>
            )}
          </div>
        )}
      </div>
    );
  }

  if (t === "error") {
    return (
      <div style={{ color: "var(--red)", fontSize: 12 }}>
        <span style={{ fontWeight: 700 }}>{p.name as string}: </span>
        {p.message as string}
      </div>
    );
  }

  if (t === "task_event") {
    const subtype = p.type as string;
    const port = p.port as string;
    const icon = TASK_EVENT_ICONS[subtype] ?? "⚙";
    const ocrTexts = (p.ocr_texts as string[] | undefined) ?? [];
    const hasOcr = subtype === "instance_status" && ocrTexts.length > 0;
    const elapsed = fmtMsValue(p.elapsed_ms ?? p.duration_ms);
    const verify = fmtMsValue(p.verify_ms);
    const target = p.target as Record<string, unknown> | undefined;
    const detail = p.detail as Record<string, unknown> | undefined;
    const stepResults = p.step_results as unknown[] | undefined;
    const errorDetails = p.error_details as Record<string, unknown> | undefined;
    const extra = p.extra as Record<string, unknown> | undefined;
    const hasExpandedDetails =
      !!p.task_run_id || !!p.phase || !!p.timeout_sec || !!p.max_attempts ||
      !!p.request_id || !!p.log_file || !!p.events_dir ||
      hasObjectContent(target) || hasObjectContent(detail) ||
      hasObjectContent(errorDetails) || hasObjectContent(extra) ||
      (Array.isArray(stepResults) && stepResults.length > 0);

    const stateColor = (s: string) =>
      s === "main_ui" ? "var(--green)"
      : s === "game_disconnected" ? "var(--red)"
      : s === "timeout" ? "var(--red)"
      : s === "login_screen" ? "var(--amber)"
      : s === "update_restart" ? "var(--amber)"
      : s === "android_home" ? "var(--blue)"
      : s === "app_loading" ? "var(--teal)"
      : s === "activity_popup" ? "var(--purple)"
      : s === "popup" ? "var(--purple)"
      : "var(--text-dim)";

    return (
      <div>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
          <span>{icon}</span>
          <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)" }}>{subtype}</span>
          {port && <span style={{ color: "var(--teal)", fontFamily: "var(--font-mono)" }}>:{port}</span>}
          {p.phase != null && <span style={{ color: "var(--purple)", fontSize: 10, fontFamily: "var(--font-mono)" }}>{String(p.phase)}</span>}

          {subtype === "task_started" && <>
            <span style={{ fontFamily: "var(--font-mono)" }}>{p.task_id as string}</span>
            {p.task_name && <span style={{ color: "var(--text-dim)" }}>{p.task_name as string}</span>}
            {typeof p.total_steps === "number" && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{p.total_steps} steps</span>}
          </>}

          {subtype === "task_completed" && <>
            <span style={{ fontFamily: "var(--font-mono)" }}>{p.task_id as string}</span>
            {elapsed && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{elapsed}</span>}
          </>}

          {(subtype === "task_failed" || subtype === "task_needs_human") && <>
            <span style={{ color: "var(--red)", fontFamily: "var(--font-mono)" }}>{p.task_id as string}</span>
            <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
              {((p.failed_step || p.reason) as string | undefined)?.slice(0, 80)}
            </span>
            {elapsed && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{elapsed}</span>}
          </>}

          {(subtype === "task_step_started" || subtype === "task_step_completed" || subtype === "task_step_failed") && <>
            <span style={{ fontFamily: "var(--font-mono)" }}>[{p.step_id as string}]</span>
            <span style={{ color: "var(--orange)" }}>{p.action as string}</span>
            {typeof p.attempt === "number" && typeof p.max_attempts === "number" &&
              <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{p.attempt as number}/{p.max_attempts as number}</span>}
            {elapsed && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{elapsed}</span>}
            {verify && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>verify {verify}</span>}
            {subtype === "task_step_failed" &&
              <span style={{ color: "var(--red)", fontSize: 11 }}>
                {(p.will_retry as boolean) ? "retry" : "stop"} {(p.message as string)?.slice(0, 60)}
              </span>}
          </>}

          {subtype === "task_denylist_triggered" && <>
            <span style={{ fontFamily: "var(--font-mono)" }}>[{p.step_id as string}]</span>
            <span style={{ color: "var(--red)" }}>敏感词: {JSON.stringify(p.matched)}</span>
          </>}

          {subtype === "instance_status" && <>
            <span style={{ color: stateColor(p.state as string), fontFamily: "var(--font-mono)" }}>{p.state as string}</span>
            {hasOcr && (
              <button onClick={() => setExpanded(!expanded)} style={{
                background: "none", border: "none", color: "var(--text-dim)",
                fontSize: 10, cursor: "pointer", padding: 0,
              }}>
                {expanded ? "▲ 收起" : `▼ OCR(${ocrTexts.length})`}
              </button>
            )}
          </>}

          {subtype === "reconnect_step" && <>
            <span style={{ color: stateColor(p.state as string), fontFamily: "var(--font-mono)" }}>{p.state as string}</span>
            <span style={{ color: "var(--text-dim)" }}>·</span>
            <span style={{ color: "var(--orange)", fontFamily: "var(--font-mono)" }}>{p.action as string}</span>
            {p.success === false && <span style={{ color: "var(--red)" }}>✗</span>}
            {p.detail != null && (
              <span style={{ color: "var(--text-muted)", fontSize: 11 }}>
                {String(p.detail).slice(0, 60)}
              </span>
            )}
          </>}

          {subtype === "reconnect_result" && <>
            <span style={{ color: stateColor(p.initial_state as string), fontFamily: "var(--font-mono)" }}>{p.initial_state as string}</span>
            <span style={{ color: "var(--text-dim)" }}>→</span>
            <span style={{ color: stateColor(p.final_state as string), fontFamily: "var(--font-mono)" }}>{p.final_state as string}</span>
            <span>{p.success === null ? "跳过" : (p.success as boolean) ? "✅" : "❌"}</span>
          </>}

          {subtype === "executor_perf" && <>
            <span style={{ color: "var(--amber)", fontFamily: "var(--font-mono)" }}>{p.operation as string}</span>
            {(p.timing as Record<string, number>)?.total_s != null && (
              <span style={{ color: "var(--teal)", fontSize: 11 }}>
                {(p.timing as Record<string, number>).total_s.toFixed(2)}s
              </span>
            )}
            {typeof p.text_count === "number" && (
              <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{p.text_count as number} texts</span>
            )}
            <button onClick={() => setExpanded(!expanded)} style={{
              background: "none", border: "none", color: "var(--text-dim)",
              fontSize: 10, cursor: "pointer", padding: 0,
            }}>
              {expanded ? "▲ 收起" : "▼ 耗时细项"}
            </button>
          </>}

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

          {subtype === "executor_startup" && <>
            <span style={{ color: "var(--green)" }}>executor 启动</span>
            {p.host && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{p.host as string}</span>}
          </>}

          {hasExpandedDetails && (
            <button onClick={() => setExpanded(!expanded)} style={{
              background: "none", border: "none", color: "var(--text-dim)",
              fontSize: 10, cursor: "pointer", padding: 0,
            }}>
              {expanded ? "▲ 细节" : "▼ 细节"}
            </button>
          )}
        </div>

        {expanded && hasOcr && (
          <div style={{ marginTop: 4, display: "flex", flexWrap: "wrap", gap: 4 }}>
            {ocrTexts.map((text, i) => (
              <span key={i} style={{
                background: "rgba(45,212,191,.08)", border: "1px solid rgba(45,212,191,.15)",
                borderRadius: 3, padding: "1px 6px",
                fontSize: 11, color: "var(--teal)", fontFamily: "var(--font-mono)",
              }}>{text}</span>
            ))}
          </div>
        )}
        {expanded && subtype === "executor_perf" && (p.timing as Record<string, number>) != null && (
          <div style={{
            marginTop: 6, display: "flex", gap: 12,
            borderTop: "1px solid var(--border)", paddingTop: 6,
            color: "var(--text-dim)", fontSize: 11, fontFamily: "var(--font-mono)",
          }}>
            <span>截图: {(p.timing as Record<string, number>).screenshot_s?.toFixed(3)}s</span>
            <span>OCR: {(p.timing as Record<string, number>).ocr_s?.toFixed(3)}s</span>
            <span>总计: {(p.timing as Record<string, number>).total_s?.toFixed(3)}s</span>
          </div>
        )}
        {expanded && hasExpandedDetails && (
          <div style={{
            marginTop: 6,
            display: "grid",
            gap: 6,
            borderTop: "1px solid var(--border)",
            paddingTop: 6,
          }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)" }}>
              {p.task_run_id != null && <span>run:{String(p.task_run_id).slice(-28)}</span>}
              {p.request_id != null && <span>req:{String(p.request_id).slice(-20)}</span>}
              {p.timeout_sec != null && <span>timeout:{String(p.timeout_sec)}s</span>}
              {p.retries != null && <span>retries:{String(p.retries)}</span>}
              {typeof p.preflight_steps === "number" && <span>preflight:{p.preflight_steps as number}</span>}
              {typeof p.main_steps === "number" && <span>main:{p.main_steps as number}</span>}
            </div>
            {(p.log_file != null || p.events_dir != null || p.adb_path != null) && (
              <JsonBlock value={{ log_file: p.log_file, events_dir: p.events_dir, adb_path: p.adb_path }} />
            )}
            {hasObjectContent(target) && <JsonBlock value={{ target }} />}
            {hasObjectContent(detail) && <JsonBlock value={{ detail }} />}
            {hasObjectContent(errorDetails) && <JsonBlock value={{ error_details: errorDetails }} />}
            {hasObjectContent(extra) && <JsonBlock value={{ extra }} />}
            {Array.isArray(stepResults) && stepResults.length > 0 && <JsonBlock value={{ step_results: stepResults }} />}
          </div>
        )}
      </div>
    );
  }

  if (t === "session_started") {
    return <div style={{ color: "var(--green)", fontSize: 12 }}>会话开始</div>;
  }

  return (
    <pre style={{ color: "var(--text-muted)", fontSize: 11, overflow: "auto", maxHeight: 80, margin: 0 }}>
      {JSON.stringify(p, null, 2)}
    </pre>
  );
}

// ── event row & task-event group ──────────────────────────────────────────────

function EventRow({ event: e, anchorId, roundsByTrace, traceComplete, isMobile }: {
  event: NormalizedEvent;
  anchorId: string | undefined;
  roundsByTrace: Record<string, number>;
  traceComplete: Set<string>;
  isMobile?: boolean;
}) {
  const color = EVENT_COLORS[e.event_type] ?? "var(--text-dim)";
  const isError = e.event_type === "error";
  const isThought = e.event_type === "thought";
  return (
    <div
      id={anchorId}
      style={{
        display: "flex",
        gap: "0.75rem",
        marginBottom: "0.4rem",
        marginLeft: isThought ? "1.5rem" : 0,
        paddingLeft: "0.75rem",
        paddingTop: isError ? 4 : 0,
        paddingBottom: isError ? 4 : 0,
        borderLeft: `${isThought ? "1px dashed" : "2px solid"} ${color}`,
        borderRadius: isError ? "0 var(--r) var(--r) 0" : 0,
        background: isError ? "rgba(248,113,113,.04)" : "transparent",
        boxShadow: isError ? "inset 0 0 12px rgba(248,113,113,.06)" : "none",
        transition: `background var(--dur) var(--ease)`,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", flexShrink: 0, paddingTop: 2, gap: 1, minWidth: 0, maxWidth: "100%" }}>
        <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)", lineHeight: 1.2 }}>{fmtTime(e.timestamp)}</span>
        {!isMobile && (
          <span style={{ color, fontSize: 9, fontFamily: "var(--font-mono)", lineHeight: 1.2, opacity: 0.8 }}>{e.event_type}</span>
        )}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <EventDetail event={e} />
        {e.trace_id && (
          <div style={{ marginTop: 4, fontSize: 10, display: "flex", alignItems: "center", gap: 6 }}>
            <Link href={`/traces/${e.trace_id}`} style={{ color: "var(--blue)", opacity: 0.6, fontFamily: "var(--font-mono)" }}>
              trace:{e.trace_id.slice(-16)}
            </Link>
            {e.trace_id && (roundsByTrace[e.trace_id] ?? 0) > 0 && (
              <span style={{
                color: traceComplete.has(e.trace_id) ? "var(--amber)" : "var(--text-dim)",
                fontSize: 10, fontFamily: "var(--font-mono)", opacity: 0.8,
              }}>
                {roundsByTrace[e.trace_id]}轮
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function TaskEventGroup({ events, anchors, roundsByTrace, traceComplete, isMobile }: {
  events: NormalizedEvent[];
  anchors: Record<string, string | undefined>;
  roundsByTrace: Record<string, number>;
  traceComplete: Set<string>;
  isMobile?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const subtypeCounts = events.reduce<Record<string, number>>((acc, e) => {
    const st = (e.payload as Record<string, unknown>).type as string | undefined;
    if (st) acc[st] = (acc[st] ?? 0) + 1;
    return acc;
  }, {});
  const sortedSubtypes = Object.entries(subtypeCounts).sort((a, b) => b[1] - a[1]);
  const firstE = events[0];
  const lastE = events[events.length - 1];
  const color = EVENT_COLORS["task_event"] ?? "var(--green)";

  if (!expanded) {
    return (
      <div
        id={anchors[firstE.event_id]}
        style={{
          display: "flex",
          gap: "0.75rem",
          marginBottom: "0.4rem",
          paddingLeft: "0.75rem",
          borderLeft: `2px dashed ${color}`,
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", flexShrink: 0, paddingTop: 2, gap: 1, minWidth: 0, maxWidth: "100%" }}>
          <span style={{ color: "var(--text-dim)", fontSize: 10, fontFamily: "var(--font-mono)", lineHeight: 1.2 }}>{fmtTime(firstE.timestamp)}</span>
          {!isMobile && (
            <span style={{ color, fontSize: 9, fontFamily: "var(--font-mono)", lineHeight: 1.2, opacity: 0.8 }}>task_event</span>
          )}
        </div>
        <div style={{ flex: 1, minWidth: 0, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
          <button onClick={() => setExpanded(true)} style={{
            background: "none", border: `1px solid ${color}`, color,
            cursor: "pointer", fontSize: 11, padding: "1px 8px", borderRadius: 3,
            fontFamily: "var(--font-mono)",
          }}>
            ▶ 展开 {events.length} 条
          </button>
          {sortedSubtypes.map(([st, n]) => (
            <span key={st} style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)" }}>
              {st}<span style={{ color: "var(--text-dim)" }}>×{n}</span>
            </span>
          ))}
          <span style={{ color: "var(--text-dim)", fontSize: 10, marginLeft: "auto", fontFamily: "var(--font-mono)" }}>
            → {fmtTime(lastE.timestamp)}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div style={{
        display: "flex", gap: "0.75rem", marginBottom: "0.2rem",
        paddingLeft: "0.75rem",
      }}>
        <div style={{ width: 70, flexShrink: 0 }} />
        <button onClick={() => setExpanded(false)} style={{
          background: "none", border: "1px solid var(--border)", color: "var(--text-dim)",
          cursor: "pointer", fontSize: 11, padding: "1px 8px", borderRadius: 3,
          fontFamily: "var(--font-mono)",
        }}>
          ▼ 折叠 {events.length} 条
        </button>
      </div>
      {events.map((e) => (
        <EventRow
          key={e.event_id}
          event={e}
          anchorId={anchors[e.event_id]}
          roundsByTrace={roundsByTrace}
          traceComplete={traceComplete}
          isMobile={isMobile}
        />
      ))}
    </div>
  );
}

// ── timeline ──────────────────────────────────────────────────────────────────

function Timeline({ events, roundsByTrace }: { events: NormalizedEvent[]; roundsByTrace: Record<string, number> }) {
  const [filter, setFilter] = useState<string>("all");
  const isMobile = useIsMobile();
  const types = Array.from(new Set(events.map((e) => e.event_type)));
  const visible = filter === "all" ? events : events.filter((e) => e.event_type === filter);

  const scrollRef = useRef<HTMLDivElement>(null);

  const traceComplete = new Set(
    events
      .filter((e) => e.event_type === "message" && (e.payload as Record<string, unknown>).role === "assistant")
      .map((e) => e.trace_id)
      .filter(Boolean) as string[]
  );

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: "0.75rem", alignItems: "center" }}>
        {["all", ...types].map((t) => (
          <button
            key={t}
            onClick={() => setFilter(t)}
            className={`tag-btn${filter === t ? " active" : ""}`}
            style={filter !== t && EVENT_COLORS[t]
              ? { color: EVENT_COLORS[t], borderColor: "var(--border)" }
              : undefined
            }
          >
            {t === "all" ? "全部" : t}
          </button>
        ))}
        <span style={{ color: "var(--text-dim)", fontSize: 11, marginLeft: "auto", fontFamily: "var(--font-mono)" }}>
          {visible.length} / {events.length}
        </span>
      </div>

      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto" }}>
        {(() => {
          type Item =
            | { kind: "single"; event: NormalizedEvent }
            | { kind: "group"; events: NormalizedEvent[] };
          const items: Item[] = [];
          let buf: NormalizedEvent[] = [];
          const flush = () => {
            if (buf.length === 0) return;
            if (buf.length >= 3) items.push({ kind: "group", events: buf });
            else buf.forEach((ev) => items.push({ kind: "single", event: ev }));
            buf = [];
          };
          for (const e of visible) {
            if (e.event_type === "task_event") buf.push(e);
            else { flush(); items.push({ kind: "single", event: e }); }
          }
          flush();

          const seenTraceIds = new Set<string>();
          const allocAnchor = (e: NormalizedEvent): string | undefined => {
            if (!e.trace_id || seenTraceIds.has(e.trace_id)) return undefined;
            seenTraceIds.add(e.trace_id);
            return `trace-${e.trace_id}`;
          };

          return items.map((item, idx) => {
            if (item.kind === "single") {
              return (
                <EventRow
                  key={item.event.event_id}
                  event={item.event}
                  anchorId={allocAnchor(item.event)}
                  roundsByTrace={roundsByTrace}
                  traceComplete={traceComplete}
                  isMobile={isMobile}
                />
              );
            }
            const anchors: Record<string, string | undefined> = {};
            item.events.forEach((e) => {
              anchors[e.event_id] = allocAnchor(e);
            });
            return (
              <TaskEventGroup
                key={`group-${idx}-${item.events[0].event_id}`}
                events={item.events}
                anchors={anchors}
                roundsByTrace={roundsByTrace}
                traceComplete={traceComplete}
                isMobile={isMobile}
              />
            );
          });
        })()}
        {visible.length === 0 && <p style={{ color: "var(--text-dim)" }}>无匹配事件</p>}
      </div>
    </div>
  );
}

// ── agent badge ───────────────────────────────────────────────────────────────

function AgentBadge({ label }: { label: string }) {
  const color = agentColor(label);
  return (
    <span className="badge" style={{ background: color + "18", color, border: `1px solid ${color}30` }}>
      {AGENT_DISPLAY[label] ?? label}
    </span>
  );
}

function BotChatComposer({
  session,
  isLatestSession,
  onSent,
}: {
  session: SessionSummary;
  isLatestSession: boolean;
  onSent: (sessionId: string) => void;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState("");
  const info = parseBotSessionId(session.id);
  const project = session.agent_id ?? session.project_id;
  const isTodaySession = !!info && info.ymd === todayYmdInShanghai();
  const canShow = isLatestSession && !!info && CHAT_PROJECTS.has(project);

  if (!canShow || !info) return null;

  const submit = async () => {
    const value = text.trim();
    if (!value || sending) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 120_000);
    setSending(true);
    setNotice("模型推理中，最长约 90 秒...");
    try {
      const result = await api.sendBotChat(project, info.userId, value, controller.signal);
      setText("");
      setNotice(`已写入 trace:${result.trace_id.slice(-16)}`);
      onSent(result.obs_session_id);
    } catch (e) {
      setNotice(chatErrorMessage(e));
    } finally {
      window.clearTimeout(timer);
      setSending(false);
    }
  };

  return (
    <div style={{
      borderTop: "1px solid var(--border)",
      paddingTop: "0.75rem",
      marginTop: "0.75rem",
      display: "grid",
      gap: 8,
    }}>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            void submit();
          }
        }}
        disabled={sending}
        placeholder={`给 ${AGENT_DISPLAY[project] ?? project} 发送消息`}
        rows={3}
        style={{
          width: "100%",
          resize: "vertical",
          minHeight: 76,
          maxHeight: 160,
          boxSizing: "border-box",
          background: "rgba(255,255,255,.03)",
          color: "var(--text)",
          border: "1px solid var(--border)",
          borderRadius: "var(--r)",
          padding: "10px 12px",
          fontSize: 13,
          lineHeight: 1.5,
          outline: "none",
          opacity: sending ? 0.7 : 1,
        }}
      />
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button
          onClick={() => void submit()}
          disabled={sending || !text.trim()}
          style={{
            border: "1px solid var(--border-hi)",
            background: sending || !text.trim() ? "rgba(255,255,255,.04)" : "var(--panel-hi)",
            color: sending || !text.trim() ? "var(--text-dim)" : "var(--text)",
            borderRadius: 4,
            padding: "6px 12px",
            fontSize: 12,
            cursor: sending || !text.trim() ? "not-allowed" : "pointer",
          }}
        >
          {sending ? "发送中..." : "发送"}
        </button>
        <span style={{ color: notice.startsWith("已写入") ? "var(--green)" : "var(--text-dim)", fontSize: 11 }}>
          {notice || `${isTodaySession ? "当前会话" : "将写入今天的新会话"} · user:${info.userId} · Ctrl/⌘ + Enter`}
        </span>
      </div>
    </div>
  );
}

// ── main page ─────────────────────────────────────────────────────────────────

export default function SessionsPage() {
  const router = useRouter();
  const isMobile = useIsMobile();
  const projectId = (router.query.project_id as string) ?? "";
  const selectedId = (router.query.session_id as string) ?? "";

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [events, setEvents] = useState<NormalizedEvent[]>([]);
  const [roundsByTrace, setRoundsByTrace] = useState<Record<string, number>>({});
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [agentFilter, setAgentFilter] = useState<string>("all");
  const [err, setErr] = useState("");
  const [livePanelWidth, setLivePanelWidth] = useState(380);
  const resizingRef = useRef(false);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!resizingRef.current) return;
      const w = Math.max(280, Math.min(800, window.innerWidth - e.clientX));
      setLivePanelWidth(w);
    };
    const onUp = () => { resizingRef.current = false; document.body.style.cursor = ""; };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
  }, []);

  function startResize() {
    resizingRef.current = true;
    document.body.style.cursor = "col-resize";
  }

  // Stable ref so SSE/polling closures always see the current selectedId
  const selectedIdRef = useRef(selectedId);
  useEffect(() => { selectedIdRef.current = selectedId; }, [selectedId]);

  // Scroll selected session into view after list loads (e.g. returning from trace page)
  const selectedItemRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (selectedId && !loadingSessions) {
      selectedItemRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [selectedId, loadingSessions]);

  useEffect(() => { setAgentFilter("all"); }, [projectId]);

  const fetchSessions = useCallback(() => {
    if (!router.isReady) return;
    setLoadingSessions(true);
    api.sessions({ project_id: projectId || undefined, limit: 100 })
      .then((list) => setSessions(list.filter((s) => s.agent_id !== "windows-executor")))
      .catch((e) => setErr(String(e)))
      .finally(() => setLoadingSessions(false));
  }, [router.isReady, projectId]);

  // Silent timeline refresh — no loading spinner, used by SSE/polling
  const refreshTimelineSilent = useCallback((id: string) => {
    if (!id) return;
    api.timeline(id).then((r) => { setEvents(r.events); setRoundsByTrace(r.rounds_by_trace); }).catch(() => {});
  }, []);

  useEffect(fetchSessions, [fetchSessions]);

  // SSE + fallback polling
  useEffect(() => {
    if (!router.isReady) return;
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    // SSE origin 经 gateway.ts 派生：网关模式同源（Caddy 不 buffer SSE），
    // 直连模式仍直连 :8000 绕过 Next dev 代理的 SSE buffering。
    const es = new EventSource(`${sseOrigin()}/api/stream`);

    es.onopen = () => {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    };

    es.onmessage = (e) => {
      if (e.data === "ingest") {
        fetchSessions();
        refreshTimelineSilent(selectedIdRef.current);
      }
    };

    // SSE disconnected — fall back to low-frequency polling of current timeline
    es.onerror = () => {
      if (!pollTimer) {
        pollTimer = setInterval(() => {
          refreshTimelineSilent(selectedIdRef.current);
        }, 8000);
      }
    };

    return () => {
      es.close();
      if (pollTimer) clearInterval(pollTimer);
    };
  }, [router.isReady, projectId, fetchSessions, refreshTimelineSilent]);

  // Load timeline with loading indicator when user navigates to a session
  useEffect(() => {
    if (!selectedId) return;
    setLoadingEvents(true);
    api.timeline(selectedId)
      .then((r) => { setEvents(r.events); setRoundsByTrace(r.rounds_by_trace); })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoadingEvents(false));
  }, [selectedId]);

  // Scroll timeline to hash anchor after events load, then clear hash
  useEffect(() => {
    if (loadingEvents || events.length === 0) return;
    const hash = window.location.hash;
    if (!hash.startsWith("#trace-")) return;
    const el = document.getElementById(hash.slice(1));
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }, [loadingEvents, events]);

  function selectSession(id: string) {
    router.push(
      { pathname: "/sessions", query: { ...(projectId ? { project_id: projectId } : {}), session_id: id } },
      undefined,
      { shallow: true }
    );
  }

  const selectSessionById = useCallback((id: string) => {
    router.push(
      { pathname: "/sessions", query: { ...(projectId ? { project_id: projectId } : {}), session_id: id } },
      undefined,
      { shallow: true }
    );
  }, [router, projectId]);

  function backToSessionList() {
    router.push(
      { pathname: "/sessions", query: projectId ? { project_id: projectId } : {} },
      undefined,
      { shallow: true }
    );
  }

  const agentKeys = Array.from(new Set(sessions.map((s) => s.agent_id ?? s.project_id).filter(Boolean)));
  const visible = agentFilter === "all"
    ? sessions
    : sessions.filter((s) => (s.agent_id ?? s.project_id) === agentFilter);

  const selectedSession = sessions.find((s) => s.id === selectedId);
  const selectedProject = selectedSession ? (selectedSession.agent_id ?? selectedSession.project_id) : "";
  const showMhxyLiveStream = !isMobile
    && !!selectedSession
    && selectedSession.project_id === "mhxy";
  const latestSameProjectSession = selectedProject
    ? sessions.find((s) => (s.agent_id ?? s.project_id) === selectedProject)
    : undefined;
  const isLatestSelectedSession = !!selectedSession && latestSameProjectSession?.id === selectedSession.id;

  return (
    <div style={{ height: "calc(100vh - 3.5rem)", display: "flex", flexDirection: "column" }}>
      {/* header */}
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "1rem", flexWrap: "wrap" }}>
        <Link href="/" style={{ color: "var(--text-muted)", fontSize: 13 }}>← 返回总览</Link>
        <span style={{ color: "var(--border-hi)" }}>|</span>
        <h1 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)", margin: 0 }}>会话时间线</h1>
        {projectId && <AgentBadge label={projectId} />}
        {err && <span style={{ color: "var(--red)", fontSize: 12 }}>{err}</span>}
      </div>

      <div style={{
        flex: 1,
        display: "flex",
        gap: "1rem",
        minHeight: 0,
        flexDirection: isMobile ? "column" : "row",
      }}>
        {/* left: session list */}
        {(!isMobile || !selectedId) && (
        <div style={{
          width: isMobile ? "100%" : 240,
          flexShrink: 0,
          display: "flex",
          flexDirection: "column",
          borderRight: isMobile ? "none" : "1px solid var(--border)",
          paddingRight: isMobile ? 0 : "1rem",
          minHeight: 0,
        }}>
          {/* agent filter */}
          {agentKeys.length > 1 && (
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: "0.75rem" }}>
              {["all", ...agentKeys].map((k) => (
                <button
                  key={k}
                  onClick={() => setAgentFilter(k)}
                  className={`tag-btn${agentFilter === k ? " active" : ""}`}
                  style={agentFilter === k
                    ? { background: agentColor(k) + "22", color: agentColor(k), borderColor: agentColor(k) + "55" }
                    : { color: "var(--text-muted)" }
                  }
                >
                  {k === "all" ? `全部 (${sessions.length})` : (AGENT_DISPLAY[k] ?? k)}
                </button>
              ))}
            </div>
          )}

          <div style={{ flex: 1, overflowY: "auto" }}>
            {loadingSessions
              ? [0,1,2,3].map((i) => <SkeletonSessionItem key={i} />)
              : visible.map((s) => {
                  const botKey = s.agent_id ?? s.project_id;
                  const color = agentColor(botKey);
                  const isSelected = selectedId === s.id;
                  return (
                    <div
                      key={s.id}
                      ref={isSelected ? selectedItemRef : undefined}
                      onClick={() => selectSession(s.id)}
                      className={`session-list-item${isSelected ? " selected" : ""}`}
                      style={{ borderLeftColor: isSelected ? "var(--blue)" : color + "66" }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 4 }}>
                        <AgentBadge label={botKey} />
                        {!projectId && s.project_id !== botKey &&
                          (AGENT_DISPLAY[s.project_id] ?? s.project_id) !== (AGENT_DISPLAY[botKey] ?? botKey) && (
                          <span style={{ color: "var(--text-dim)", fontSize: 10 }}>{AGENT_DISPLAY[s.project_id] ?? s.project_id}</span>
                        )}
                      </div>
                      <div style={{
                        color: isSelected ? "var(--blue)" : "var(--text-muted)",
                        fontSize: 11, fontFamily: "var(--font-mono)",
                      }} title={s.id}>
                        {shortId(s.id)}
                      </div>
                      <div style={{ color: "var(--text-dim)", fontSize: 10, marginTop: 2 }}>
                        {fmtDate(s.started_at)}
                      </div>
                    </div>
                  );
                })
            }
            {!loadingSessions && visible.length === 0 && (
              <p style={{ color: "var(--text-dim)", fontSize: 12 }}>无会话数据</p>
            )}
          </div>
        </div>
        )}

        {/* right: timeline */}
        {(!isMobile || selectedId) && (
        <div style={{ flex: 1, minWidth: 0, display: "flex", minHeight: 0, gap: "1rem" }}>
          <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
            {isMobile && selectedId && (
              <button
                onClick={backToSessionList}
                style={{
                  background: "none",
                  border: "none",
                  color: "var(--blue)",
                  fontSize: 12,
                  marginBottom: "0.5rem",
                  padding: 0,
                  cursor: "pointer",
                  alignSelf: "flex-start",
                }}
              >
                ← 返回会话列表
              </button>
            )}
            {selectedSession && (
              <div style={{
                display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap",
                marginBottom: "0.75rem", paddingBottom: "0.75rem",
                borderBottom: "1px solid var(--border)",
              }}>
                <AgentBadge label={selectedSession.agent_id ?? selectedSession.project_id} />
                <CopyableId id={selectedSession.id} truncate={36} />
                <span style={{ color: "var(--text-dim)", fontSize: 11, marginLeft: "auto" }}>
                  {fmtDate(selectedSession.started_at)}
                </span>
              </div>
            )}
            {!selectedId && (
              <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
                <p style={{ color: "var(--text-dim)" }}>← 选择左侧会话查看时间线</p>
              </div>
            )}
            {loadingEvents && <p style={{ color: "var(--text-dim)" }}>加载中…</p>}
            {!loadingEvents && selectedId && events.length > 0 && (
              <>
                <div style={{ flex: 1, minHeight: 0 }}>
                  <Timeline events={events} roundsByTrace={roundsByTrace} />
                </div>
                {selectedSession && (
                  <BotChatComposer
                    session={selectedSession}
                    isLatestSession={isLatestSelectedSession}
                    onSent={(id) => {
                      fetchSessions();
                      if (id === selectedId) {
                        refreshTimelineSilent(id);
                      } else {
                        selectSessionById(id);
                      }
                    }}
                  />
                )}
              </>
            )}
            {!loadingEvents && selectedId && events.length === 0 && (
              <p style={{ color: "var(--text-dim)" }}>该会话暂无事件</p>
            )}
          </div>
          {showMhxyLiveStream && (
            <>
              <div
                onMouseDown={(e) => { e.preventDefault(); startResize(); }}
                style={{
                  width: 4,
                  cursor: "col-resize",
                  flexShrink: 0,
                  borderRadius: 2,
                  background: "var(--border)",
                  transition: "background 0.15s",
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = "var(--blue)"; }}
                onMouseLeave={(e) => { if (!resizingRef.current) e.currentTarget.style.background = "var(--border)"; }}
              />
              <MhxyLiveStreamPanel width={livePanelWidth} />
            </>
          )}
        </div>
        )}
      </div>
    </div>
  );
}

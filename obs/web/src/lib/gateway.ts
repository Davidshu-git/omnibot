/**
 * 网关 URL 派生 —— 单一出口，决定各子连接走 TLS 网关还是直连。
 *
 * 判定依据：页面是否 https。https ⇒ 经 obs/caddy 网关访问（Secure Context，
 * WebCodecs 可用），所有子连接走网关同源/相对路径；http ⇒ 老入口
 * （localhost:3100 开发 or 直连 192.168.1.100:3100），维持历史直连行为，零回归。
 *
 * 与 obs/caddy/Caddyfile 形成契约：
 *   - exec WS  → :8443 /exec-ws/*  → 192.168.100.149:8765
 *   - scrcpy   → :8444 根路径直通  → 192.168.100.149:8000
 *   - SSE/API  → :8443 同源 /api/* → api:8000（Caddy 不 buffer SSE）
 * 改任一侧路径/端口，另一侧需联动。
 */

const DIRECT_EXEC_WS = process.env.NEXT_PUBLIC_EXECUTOR_WS_BASE || "ws://192.168.100.149:8765";
const DIRECT_SCRCPY = "http://192.168.100.149:8000";

/** 是否经 TLS 网关访问（https ⇒ 是）。SSR 阶段无 window，按直连处理。 */
export function isGatewayMode(): boolean {
  return typeof window !== "undefined" && window.location.protocol === "https:";
}

/**
 * executor 输入 / 推流 WS 基址（不含末尾斜杠）。
 * 调用方按原样拼 `/ws/input`、`/ws/stream/{port}`。
 */
export function execWsBase(): string {
  if (isGatewayMode()) return `wss://${window.location.host}/exec-ws`;
  return DIRECT_EXEC_WS;
}

/** executor HTTP 基址（由 WS 基址换协议得到，供非 WS 的 executor 调用复用）。 */
export function execHttpBase(): string {
  return execWsBase().replace(/^ws(s)?:\/\//, "http$1://");
}

/** ws-scrcpy 基址（网关模式走独立 :8444 端口，根路径直通）。 */
export function scrcpyBase(): string {
  if (isGatewayMode()) return `https://${window.location.hostname}:8444`;
  return DIRECT_SCRCPY;
}

/**
 * SSE EventSource 的 origin。
 * 网关模式：同源（Caddy /api/* 直连 api:8000 且不 buffer SSE）。
 * 直连模式：维持历史行为，直连 :8000 绕过 Next dev 代理的 SSE buffering。
 */
export function sseOrigin(): string {
  if (typeof window === "undefined") return "http://localhost:8000";
  if (isGatewayMode()) return window.location.origin;
  return `${window.location.protocol}//${window.location.hostname}:8000`;
}

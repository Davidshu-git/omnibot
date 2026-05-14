/**
 * WebSocket 输入通道 — 直连 executor /ws/input，使用二进制协议发 tap/swipe。
 * 跳过 obs-api HTTP 代理，延迟比 HTTP REST 低 ~15-30ms。
 */

export type InputAction =
  | { type: "tap"; ports: string[]; px: number; py: number }
  | { type: "swipe"; ports: string[]; x1: number; y1: number; x2: number; y2: number; durationMs: number };

const EXECUTOR_WS_URL = `ws://192.168.100.149:8765/ws/input`;

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
const pending: Array<{ action: InputAction; resolve: (ok: boolean) => void }> = [];
const MAX_QUEUE = 20;

function encodeBinary(action: InputAction): Uint8Array {
  const ports = action.ports.map(Number);
  const portCount = ports.length;
  const headerLen = 2 + portCount * 2; // type + count + ports
  const payloadLen = action.type === "tap" ? 4 : 10;
  const buf = new Uint8Array(headerLen + payloadLen);
  let off = 0;

  buf[off++] = action.type === "tap" ? 1 : 2;
  buf[off++] = portCount;

  for (const p of ports) {
    buf[off++] = p & 0xff;
    buf[off++] = (p >> 8) & 0xff;
  }

  if (action.type === "tap") {
    buf[off++] = action.px & 0xff;
    buf[off++] = (action.px >> 8) & 0xff;
    buf[off++] = action.py & 0xff;
    buf[off++] = (action.py >> 8) & 0xff;
  } else {
    const writeU16 = (v: number) => {
      buf[off++] = v & 0xff;
      buf[off++] = (v >> 8) & 0xff;
    };
    writeU16(action.x1);
    writeU16(action.y1);
    writeU16(action.x2);
    writeU16(action.y2);
    writeU16(action.durationMs);
  }

  return buf;
}

function processQueue() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  while (pending.length > 0) {
    const item = pending.shift()!;
    ws!.send(encodeBinary(item.action));
    item.resolve(true); // fire-and-forget: ACK 不重要
  }
}

function connect() {
  if (ws && ws.readyState <= WebSocket.OPEN) return;

  ws = new WebSocket(EXECUTOR_WS_URL);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => processQueue();

  ws.onclose = () => {
    reconnectTimer = setTimeout(connect, 3000);
  };

  ws.onerror = () => {
    ws?.close();
  };

  // 收到 ACK（单字节），忽略
  ws.onmessage = () => {};
}

// 预热连接
connect();

/**
 * 通过 WebSocket 发送输入操作。fire-and-forget，不等待 ACK。
 * 队列满时丢弃最旧的操作。
 */
export function sendInput(action: InputAction) {
  if (pending.length >= MAX_QUEUE) {
    pending.shift();
  }
  pending.push({ action, resolve: () => {} });
  processQueue();
}

/** 关闭连接（页面卸载时调用）。 */
export function closeInputWs() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  ws?.close();
  ws = null;
}

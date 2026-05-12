import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, type MhxyExecutorInstances, type MhxyInstanceDetail } from "@/lib/api";
import { fmtTime } from "@/lib/format";

const ROLE_LABEL: Record<string, string> = {
  leader: "队长",
  member: "队员",
  standalone: "单机",
};

const ROLE_COLOR: Record<string, string> = {
  leader: "var(--amber)",
  member: "var(--blue)",
  standalone: "var(--text-muted)",
};

type ScreenshotState = "idle" | "loading" | "error" | string; // string = base64

function CheckIcon({ ok }: { ok: boolean | null }) {
  if (ok === null || ok === undefined) return <span style={{ color: "var(--text-dim)" }}>—</span>;
  return ok
    ? <span style={{ color: "var(--green)", fontWeight: 700 }}>✓</span>
    : <span style={{ color: "var(--red)", fontWeight: 700 }}>✗</span>;
}

// ---------------------------------------------------------------------------
// Screenshot modal (full size) — with optional broadcast-click mode
// ---------------------------------------------------------------------------

type BroadcastStatus =
  | null
  | { pending: true }
  | { pending: false; ok: number; fail: number; px: number; py: number };

const WS_SCRCPY_BASE = "http://192.168.100.149:8000";

function ScreenshotModal({
  port,
  state,
  allPorts,
  onClose,
  onRefreshScreenshot,
}: {
  port: string;
  state: ScreenshotState;
  allPorts: string[];
  onClose: () => void;
  onRefreshScreenshot: (port: string) => Promise<void>;
}) {
  const [viewMode, setViewMode] = useState<"screenshot" | "stream">("screenshot");
  const [broadcastMode, setBroadcastMode] = useState(false);
  const [broadcastStatus, setBroadcastStatus] = useState<BroadcastStatus>(null);
  const [hoverXY, setHoverXY] = useState<{ x: number; y: number } | null>(null);
  const [clickRipple, setClickRipple] = useState<{ pctX: number; pctY: number; key: number } | null>(null);
  const [deviceSize, setDeviceSize] = useState<{ w: number; h: number }>({ w: 720, h: 1280 });
  const imgRef = useRef<HTMLImageElement>(null);
  const tapPendingRef = useRef(false);

  // ADB device ID: MuMu emulator port is odd (5557), ADB port is port-1 (5556)
  const adbDevice = `emulator-${parseInt(port) - 1}`;
  const streamUrl = `${WS_SCRCPY_BASE}/embed.html?device=${adbDevice}`;

  // Extract device resolution from screenshot for accurate coordinate mapping
  useEffect(() => {
    const isImage = state !== "loading" && state !== "error" && state !== "idle";
    if (!isImage) return;
    const img = new Image();
    img.onload = () => setDeviceSize({ w: img.naturalWidth, h: img.naturalHeight });
    img.src = `data:image/jpeg;base64,${state}`;
  }, [state]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (broadcastMode) { setBroadcastMode(false); setBroadcastStatus(null); }
        else onClose();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose, broadcastMode]);

  const handleImgMouseMove = useCallback((e: React.MouseEvent<HTMLImageElement>) => {
    if (!imgRef.current) return;
    const rect = imgRef.current.getBoundingClientRect();
    setHoverXY({
      x: Math.round((e.clientX - rect.left) / rect.width * imgRef.current.naturalWidth),
      y: Math.round((e.clientY - rect.top) / rect.height * imgRef.current.naturalHeight),
    });
  }, []);

  const handleImgClick = useCallback((e: React.MouseEvent<HTMLImageElement>) => {
    if (!imgRef.current) return;
    e.stopPropagation();
    const rect = imgRef.current.getBoundingClientRect();
    const px = Math.round((e.clientX - rect.left) / rect.width * imgRef.current.naturalWidth);
    const py = Math.round((e.clientY - rect.top) / rect.height * imgRef.current.naturalHeight);
    const pctX = (e.clientX - rect.left) / rect.width * 100;
    const pctY = (e.clientY - rect.top) / rect.height * 100;
    setClickRipple({ pctX, pctY, key: Date.now() });
    if (tapPendingRef.current) return;
    const targets = broadcastMode ? allPorts : [port];
    tapPendingRef.current = true;
    setBroadcastStatus({ pending: true });
    api.mhxyExecutorBatchTap(targets, px, py)
      .then((d) => {
        const ok = Object.values(d.results).filter(Boolean).length;
        setBroadcastStatus({ pending: false, ok, fail: targets.length - ok, px, py });
        setTimeout(() => onRefreshScreenshot(port), 200);
      })
      .catch(() => {
        setBroadcastStatus({ pending: false, ok: 0, fail: targets.length, px, py });
      })
      .finally(() => { tapPendingRef.current = false; });
  }, [broadcastMode, allPorts, port, onRefreshScreenshot]);

  const handleStreamMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    setHoverXY({
      x: Math.round((e.clientX - rect.left) / rect.width * deviceSize.w),
      y: Math.round((e.clientY - rect.top) / rect.height * deviceSize.h),
    });
  }, [deviceSize]);

  const handleStreamClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    const px = Math.round((e.clientX - rect.left) / rect.width * deviceSize.w);
    const py = Math.round((e.clientY - rect.top) / rect.height * deviceSize.h);
    const pctX = (e.clientX - rect.left) / rect.width * 100;
    const pctY = (e.clientY - rect.top) / rect.height * 100;
    setClickRipple({ pctX, pctY, key: Date.now() });
    if (tapPendingRef.current) return;
    const targets = broadcastMode ? allPorts : [port];
    tapPendingRef.current = true;
    setBroadcastStatus({ pending: true });
    api.mhxyExecutorBatchTap(targets, px, py)
      .then((d) => {
        const ok = Object.values(d.results).filter(Boolean).length;
        setBroadcastStatus({ pending: false, ok, fail: targets.length - ok, px, py });
      })
      .catch(() => {
        setBroadcastStatus({ pending: false, ok: 0, fail: targets.length, px, py });
      })
      .finally(() => { tapPendingRef.current = false; });
  }, [deviceSize, broadcastMode, allPorts, port]);

  const isImage = state !== "loading" && state !== "error" && state !== "idle";
  const canBroadcast = allPorts.length > 1;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0,
        background: "rgba(0,0,0,0.88)",
        display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center",
        zIndex: 9999,
      }}
    >
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 10 }}>
        {/* 截图 / 实时流 切换 */}
        <div
          onClick={(e) => e.stopPropagation()}
          style={{ display: "flex", gap: 4 }}
        >
          {(["screenshot", "stream"] as const).map((m) => (
            <button
              key={m}
              onClick={() => { setViewMode(m); setBroadcastMode(false); setBroadcastStatus(null); }}
              style={{
                padding: "3px 10px",
                borderRadius: "var(--r-sm)",
                border: `1px solid ${viewMode === m ? "var(--blue)" : "var(--border-hi)"}`,
                background: viewMode === m ? "rgba(99,179,237,0.15)" : "transparent",
                color: viewMode === m ? "var(--blue)" : "var(--text-muted)",
                fontSize: 11, cursor: "pointer",
              }}
            >
              {m === "screenshot" ? "📷 截图" : "📺 实时流"}
            </button>
          ))}
        </div>

        <span style={{ color: "var(--text-muted)", fontSize: 12 }}>
          端口 {port}
          {broadcastMode
            ? ` — 点击广播到全部 ${allPorts.length} 个实例，Esc 退出广播模式`
            : viewMode === "screenshot"
              ? " — 点击截图操作当前实例，点击外侧或 Esc 关闭"
              : ` — 点击操作当前实例（${deviceSize.w}×${deviceSize.h}），点击外侧关闭`}
        </span>

        {canBroadcast && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setBroadcastMode((m) => !m);
              setBroadcastStatus(null);
              setHoverXY(null);
            }}
            style={{
              padding: "3px 10px",
              borderRadius: "var(--r-sm)",
              border: `1px solid ${broadcastMode ? "var(--amber)" : "var(--border-hi)"}`,
              background: broadcastMode ? "rgba(246,173,85,0.15)" : "transparent",
              color: broadcastMode ? "var(--amber)" : "var(--text-muted)",
              fontSize: 11, cursor: "pointer",
            }}
          >
            {broadcastMode ? "📡 广播模式 ON" : "📡 广播点击"}
          </button>
        )}
      </div>

      {/* Content */}
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: "90vw", maxHeight: "85vh", display: "flex", alignItems: "center", justifyContent: "center" }}
      >
        {viewMode === "stream" ? (
          <div style={{ position: "relative", display: "inline-block" }}>
            <iframe
              src={streamUrl}
              style={{
                width: "720px", height: "480px",
                border: "1px solid var(--border-hi)",
                borderRadius: 4,
                display: "block",
                pointerEvents: "none",
              }}
              allow="autoplay"
            />
            {/* Transparent overlay: captures clicks and sends ADB tap (with broadcast support) */}
            <div
              style={{
                position: "absolute", inset: 0,
                cursor: "crosshair",
                borderRadius: 4,
              }}
              onMouseMove={handleStreamMouseMove}
              onMouseLeave={() => setHoverXY(null)}
              onClick={handleStreamClick}
            >
              {clickRipple && (
                <div
                  key={clickRipple.key}
                  className="broadcast-ripple"
                  style={{ left: `${clickRipple.pctX}%`, top: `${clickRipple.pctY}%` }}
                  onAnimationEnd={() => setClickRipple(null)}
                />
              )}
              {hoverXY && (
                <div style={{
                  position: "absolute", bottom: 6, left: 6,
                  background: "rgba(0,0,0,0.75)",
                  color: "var(--amber)",
                  fontFamily: "var(--font-mono)", fontSize: 11,
                  padding: "2px 8px", borderRadius: 4,
                  pointerEvents: "none", userSelect: "none",
                }}>
                  {hoverXY.x}, {hoverXY.y}
                </div>
              )}
            </div>
          </div>
        ) : (
          <>
            {state === "loading" && (
              <span style={{ color: "var(--text-dim)", fontSize: 13 }}>加载中…</span>
            )}
            {state === "error" && (
              <span style={{ color: "var(--red)", fontSize: 13 }}>截图获取失败</span>
            )}
            {isImage && (
              <div style={{ position: "relative", display: "inline-block" }}>
                <img
                  ref={imgRef}
                  src={`data:image/jpeg;base64,${state}`}
                  alt={`port-${port}-screenshot`}
                  style={{
                    maxWidth: "100%", maxHeight: "85vh",
                    borderRadius: 4, display: "block",
                    cursor: "crosshair",
                  }}
                  onMouseMove={handleImgMouseMove}
                  onMouseLeave={() => setHoverXY(null)}
                  onClick={handleImgClick}
                />
                {clickRipple && (
                  <div
                    key={clickRipple.key}
                    className="broadcast-ripple"
                    style={{ left: `${clickRipple.pctX}%`, top: `${clickRipple.pctY}%` }}
                    onAnimationEnd={() => setClickRipple(null)}
                  />
                )}
                {hoverXY && (
                  <div style={{
                    position: "absolute", bottom: 6, left: 6,
                    background: "rgba(0,0,0,0.75)",
                    color: "var(--amber)",
                    fontFamily: "var(--font-mono)", fontSize: 11,
                    padding: "2px 8px", borderRadius: 4,
                    pointerEvents: "none", userSelect: "none",
                  }}>
                    {hoverXY.x}, {hoverXY.y}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      {/* Status bar — always occupies space to prevent layout shift */}
      <div style={{ marginTop: 10, fontSize: 12, fontFamily: "var(--font-mono)", height: 20, display: "flex", alignItems: "center", gap: 12 }}>
        {broadcastStatus ? (broadcastStatus.pending ? (
          <span style={{ color: "var(--text-dim)" }}>
            {broadcastMode ? `广播中… (${allPorts.length} 个实例)` : "点击中…"}
          </span>
        ) : (
          <span>
            <span style={{ color: broadcastStatus.fail === 0 ? "var(--green)" : "var(--amber)" }}>
              ✓ {broadcastStatus.ok} 成功
            </span>
            {broadcastStatus.fail > 0 && (
              <span style={{ color: "var(--red)", marginLeft: 8 }}>✗ {broadcastStatus.fail} 失败</span>
            )}
            <span style={{ color: "var(--text-dim)", marginLeft: 12 }}>
              @ ({broadcastStatus.px}, {broadcastStatus.py})
            </span>
          </span>
        )) : (
          <span style={{ color: "var(--text-dim)" }}>&nbsp;</span>
        )}
        {viewMode === "stream" && (
          <span style={{ color: "var(--text-dim)", marginLeft: "auto" }}>
            {adbDevice} · <a href={streamUrl} target="_blank" rel="noreferrer" style={{ color: "var(--blue)", textDecoration: "none" }}>在新标签打开 ↗</a>
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// List view components
// ---------------------------------------------------------------------------

const LIST_COLS = "60px 80px 56px 48px 48px 48px 64px 1fr 120px";
const LIST_HEADERS = ["端口", "门派", "身份", "ADB", "截图", "OCR", "OCR耗时", "状态", "预览"];

function InstanceRow({
  inst,
  screenshot,
  onScreenshot,
}: {
  inst: MhxyInstanceDetail;
  screenshot: ScreenshotState;
  onScreenshot: (port: string) => void;
}) {
  const statusColor = inst.healthy === true
    ? "var(--green)"
    : inst.healthy === false
    ? "var(--red)"
    : "var(--text-muted)";

  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: LIST_COLS,
      gap: "0.5rem",
      alignItems: "center",
      padding: "0.6rem 0.75rem",
      borderRadius: "var(--r-sm)",
      background: "var(--bg2)",
      border: "1px solid var(--border)",
      fontSize: 13,
    }}>
      <span style={{ color: "var(--text)", fontFamily: "var(--font-mono)", fontWeight: 600 }}>
        {inst.port}
      </span>
      <span style={{ color: "var(--text)" }}>{inst.school || "—"}</span>
      <span style={{ color: ROLE_COLOR[inst.role] ?? "var(--text-muted)", fontSize: 12 }}>
        {ROLE_LABEL[inst.role] ?? inst.role || "—"}
      </span>
      <span style={{ textAlign: "center" }}><CheckIcon ok={inst.adb} /></span>
      <span style={{ textAlign: "center" }}><CheckIcon ok={inst.screenshot} /></span>
      <span style={{ textAlign: "center" }}><CheckIcon ok={inst.ocr} /></span>
      <span style={{ color: "var(--text-dim)", fontFamily: "var(--font-mono)", fontSize: 12 }}>
        {inst.latency_ms !== null && inst.latency_ms !== undefined ? `${inst.latency_ms} ms` : "—"}
      </span>
      <span style={{
        color: inst.error ? "var(--red)" : statusColor,
        fontSize: 12,
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
      }}>
        {inst.error || (inst.healthy === true ? "正常" : inst.healthy === false ? "异常" : "未知")}
      </span>
      <div
        title="点击放大查看截图"
        onClick={() => onScreenshot(inst.port)}
        style={{
          width: "100%",
          aspectRatio: "16/9",
          background: "#0d0d0d",
          borderRadius: "var(--r-sm)",
          overflow: "hidden",
          cursor: "pointer",
          border: "1px solid var(--border)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          transition: "border-color 0.15s",
        }}
        onMouseEnter={(e) => (e.currentTarget.style.borderColor = "var(--blue)")}
        onMouseLeave={(e) => (e.currentTarget.style.borderColor = "var(--border)")}
      >
        {screenshot === "idle" && (
          <span style={{ color: "var(--text-dim)", fontSize: 16, lineHeight: 1 }}>📷</span>
        )}
        {screenshot === "loading" && (
          <span style={{ color: "var(--text-dim)", fontSize: 10 }}>加载中</span>
        )}
        {screenshot === "error" && (
          <span style={{ color: "var(--red)", fontSize: 10 }}>失败</span>
        )}
        {screenshot !== "idle" && screenshot !== "loading" && screenshot !== "error" && (
          <img
            src={`data:image/jpeg;base64,${screenshot}`}
            alt={`port-${inst.port}`}
            style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
          />
        )}
      </div>
    </div>
  );
}

function ColumnHeaders() {
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: LIST_COLS,
      gap: "0.5rem",
      padding: "0 0.75rem",
      marginBottom: "0.35rem",
    }}>
      {LIST_HEADERS.map((h, i) => (
        <span key={i} className="stat-label" style={{ fontSize: 11 }}>{h}</span>
      ))}
    </div>
  );
}

function GroupBlock({
  groupId,
  instances,
  screenshots,
  onScreenshot,
}: {
  groupId: number;
  instances: MhxyInstanceDetail[];
  screenshots: Record<string, ScreenshotState>;
  onScreenshot: (port: string) => void;
}) {
  const leader = instances.find((i) => i.role === "leader");
  const members = instances.filter((i) => i.role !== "leader");
  const allOk = instances.every((i) => i.healthy === true);
  const anyFail = instances.some((i) => i.healthy === false);
  const badgeColor = allOk ? "var(--green)" : anyFail ? "var(--red)" : "var(--amber)";

  return (
    <div style={{ marginBottom: "1.25rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: "0.5rem" }}>
        <span style={{ width: 8, height: 8, borderRadius: 999, background: badgeColor, flexShrink: 0 }} />
        <span style={{ color: "var(--text)", fontWeight: 600, fontSize: 13 }}>
          第 {groupId + 1} 组
        </span>
        {leader && (
          <span style={{ color: "var(--text-dim)", fontSize: 12 }}>
            队长：{leader.school || leader.port}
          </span>
        )}
      </div>
      <ColumnHeaders />
      <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
        {leader && <InstanceRow inst={leader} screenshot={screenshots[leader.port] ?? "idle"} onScreenshot={onScreenshot} />}
        {members.map((m) => <InstanceRow key={m.port} inst={m} screenshot={screenshots[m.port] ?? "idle"} onScreenshot={onScreenshot} />)}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Grid view components
// ---------------------------------------------------------------------------

// Returns the pixel bounds of the actual image content inside an objectFit:contain img element.
function getContainBounds(img: HTMLImageElement) {
  const rect = img.getBoundingClientRect();
  const imgAspect = img.naturalWidth / img.naturalHeight;
  const elemAspect = rect.width / rect.height;
  let cw: number, ch: number, cx: number, cy: number;
  if (imgAspect > elemAspect) {
    cw = rect.width; ch = rect.width / imgAspect;
    cx = rect.left; cy = rect.top + (rect.height - ch) / 2;
  } else {
    ch = rect.height; cw = rect.height * imgAspect;
    cx = rect.left + (rect.width - cw) / 2; cy = rect.top;
  }
  return { cx, cy, cw, ch };
}

function ScreenshotCard({
  inst,
  screenshot,
  onClick,
  tapMode = false,
  broadcastMode = false,
  allPorts = [],
}: {
  inst: MhxyInstanceDetail;
  screenshot: ScreenshotState;
  onClick: () => void;
  tapMode?: boolean;
  broadcastMode?: boolean;
  allPorts?: string[];
}) {
  const [tapStatus, setTapStatus] = useState<{ pending: boolean; ok?: number; fail?: number } | null>(null);
  const tapPendingRef = useRef(false);
  const [ripple, setRipple] = useState<{ pctX: number; pctY: number; key: number } | null>(null);
  const [hoverCoord, setHoverCoord] = useState<{ x: number; y: number } | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);

  const isImageLoaded = screenshot !== "idle" && screenshot !== "loading" && screenshot !== "error";

  const handleImgAreaMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (!tapMode || !imgRef.current || !isImageLoaded) return;
    const { cx, cy, cw, ch } = getContainBounds(imgRef.current);
    const rx = e.clientX - cx;
    const ry = e.clientY - cy;
    if (rx < 0 || rx > cw || ry < 0 || ry > ch) { setHoverCoord(null); return; }
    setHoverCoord({
      x: Math.round(rx / cw * imgRef.current.naturalWidth),
      y: Math.round(ry / ch * imgRef.current.naturalHeight),
    });
  }, [tapMode, isImageLoaded]);

  const handleImgAreaClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (!tapMode || !imgRef.current || !isImageLoaded) return;
    const { cx, cy, cw, ch } = getContainBounds(imgRef.current);
    const rx = e.clientX - cx;
    const ry = e.clientY - cy;
    if (rx < 0 || rx > cw || ry < 0 || ry > ch) return; // black bar → bubble up to card onClick (modal)
    e.stopPropagation();
    const px = Math.round(rx / cw * imgRef.current.naturalWidth);
    const py = Math.round(ry / ch * imgRef.current.naturalHeight);
    const containerRect = e.currentTarget.getBoundingClientRect();
    setRipple({
      pctX: (e.clientX - containerRect.left) / containerRect.width * 100,
      pctY: (e.clientY - containerRect.top) / containerRect.height * 100,
      key: Date.now(),
    });
    if (tapPendingRef.current) return;
    const targets = broadcastMode ? allPorts : [inst.port];
    tapPendingRef.current = true;
    setTapStatus({ pending: true });
    api.mhxyExecutorBatchTap(targets, px, py)
      .then((d) => {
        const ok = Object.values(d.results).filter(Boolean).length;
        setTapStatus({ pending: false, ok, fail: targets.length - ok });
        setTimeout(() => setTapStatus(null), 2500);
      })
      .catch(() => {
        setTapStatus({ pending: false, ok: 0, fail: targets.length });
        setTimeout(() => setTapStatus(null), 2500);
      })
      .finally(() => { tapPendingRef.current = false; });
  }, [tapMode, isImageLoaded, broadcastMode, allPorts, inst.port]);

  const statusColor = inst.healthy === true
    ? "var(--green)"
    : inst.healthy === false
    ? "var(--red)"
    : "var(--text-muted)";

  return (
    <div
      onClick={onClick}
      style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--r-sm)",
        overflow: "hidden",
        cursor: "pointer",
        background: "var(--bg2)",
        transition: "border-color 0.15s",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = "var(--blue)")}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = "var(--border)")}
    >
      {/* header */}
      <div style={{
        padding: "0.35rem 0.6rem",
        display: "flex", alignItems: "center", gap: 6,
        borderBottom: "1px solid var(--border)",
      }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text)", fontWeight: 600 }}>
          {inst.port}
        </span>
        {inst.school && (
          <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{inst.school}</span>
        )}
        <span style={{ fontSize: 10, color: ROLE_COLOR[inst.role] ?? "var(--text-muted)", marginLeft: 2 }}>
          {ROLE_LABEL[inst.role] ?? inst.role}
        </span>
        <span style={{
          width: 7, height: 7, borderRadius: 999,
          background: statusColor, marginLeft: "auto", flexShrink: 0,
        }} />
      </div>

      {/* screenshot area — fixed aspect ratio 16:9 */}
      <div
        style={{
          background: "#0d0d0d",
          aspectRatio: "16/9",
          display: "flex", alignItems: "center", justifyContent: "center",
          position: "relative",
          cursor: tapMode && isImageLoaded ? "crosshair" : undefined,
        }}
        onMouseMove={handleImgAreaMouseMove}
        onMouseLeave={() => setHoverCoord(null)}
        onClick={handleImgAreaClick}
      >
        {screenshot === "idle" && (
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>等待加载</span>
        )}
        {screenshot === "loading" && (
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>加载中…</span>
        )}
        {screenshot === "error" && (
          <span style={{ color: "var(--red)", fontSize: 11 }}>获取失败</span>
        )}
        {isImageLoaded && (
          <img
            ref={imgRef}
            src={`data:image/jpeg;base64,${screenshot}`}
            alt={`port-${inst.port}`}
            style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
          />
        )}
        {/* Tap ripple */}
        {ripple && (
          <div
            key={ripple.key}
            className="broadcast-ripple"
            style={{ left: `${ripple.pctX}%`, top: `${ripple.pctY}%` }}
            onAnimationEnd={() => setRipple(null)}
          />
        )}
        {/* Hover coordinate */}
        {tapMode && hoverCoord && (
          <div style={{
            position: "absolute", bottom: 4, left: 4,
            background: "rgba(0,0,0,0.75)", color: "var(--amber)",
            fontFamily: "var(--font-mono)", fontSize: 10,
            padding: "1px 6px", borderRadius: 3,
            pointerEvents: "none", userSelect: "none",
          }}>
            {hoverCoord.x}, {hoverCoord.y}
          </div>
        )}
        {/* Tap result overlay */}
        {tapStatus && (
          <div style={{
            position: "absolute", top: 4, right: 4,
            background: "rgba(0,0,0,0.8)",
            fontFamily: "var(--font-mono)", fontSize: 10,
            padding: "2px 6px", borderRadius: 3,
            pointerEvents: "none",
            color: tapStatus.pending ? "var(--text-dim)" : tapStatus.fail === 0 ? "var(--green)" : "var(--amber)",
          }}>
            {tapStatus.pending
              ? (broadcastMode ? `广播 ${allPorts.length}…` : "…")
              : `✓${tapStatus.ok}${tapStatus.fail ? ` ✗${tapStatus.fail}` : ""}`}
          </div>
        )}
      </div>

      {/* health badges */}
      <div style={{
        padding: "0.3rem 0.6rem",
        display: "flex", gap: 8, fontSize: 11, color: "var(--text-dim)",
        borderTop: "1px solid var(--border)",
      }}>
        <span>ADB <CheckIcon ok={inst.adb} /></span>
        <span>截图 <CheckIcon ok={inst.screenshot} /></span>
        <span>OCR <CheckIcon ok={inst.ocr} /></span>
        {inst.latency_ms !== null && inst.latency_ms !== undefined && (
          <span style={{ marginLeft: "auto", fontFamily: "var(--font-mono)" }}>{inst.latency_ms} ms</span>
        )}
      </div>
    </div>
  );
}

function GroupBlockGrid({
  groupId,
  instances,
  screenshots,
  onCardClick,
  tapMode,
  broadcastMode,
  allPorts,
}: {
  groupId: number;
  instances: MhxyInstanceDetail[];
  screenshots: Record<string, ScreenshotState>;
  onCardClick: (port: string) => void;
  tapMode: boolean;
  broadcastMode: boolean;
  allPorts: string[];
}) {
  const leader = instances.find((i) => i.role === "leader");
  const members = instances.filter((i) => i.role !== "leader");
  const allOk = instances.every((i) => i.healthy === true);
  const anyFail = instances.some((i) => i.healthy === false);
  const badgeColor = allOk ? "var(--green)" : anyFail ? "var(--red)" : "var(--amber)";
  const ordered = leader ? [leader, ...members] : members;

  return (
    <div style={{ marginBottom: "1.5rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: "0.75rem" }}>
        <span style={{ width: 8, height: 8, borderRadius: 999, background: badgeColor, flexShrink: 0 }} />
        <span style={{ color: "var(--text)", fontWeight: 600, fontSize: 13 }}>第 {groupId + 1} 组</span>
        {leader && (
          <span style={{ color: "var(--text-dim)", fontSize: 12 }}>队长：{leader.school || leader.port}</span>
        )}
      </div>
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
        gap: "0.75rem",
      }}>
        {ordered.map((inst) => (
          <ScreenshotCard
            key={inst.port}
            inst={inst}
            screenshot={screenshots[inst.port] ?? "idle"}
            onClick={() => onCardClick(inst.port)}
            tapMode={tapMode}
            broadcastMode={broadcastMode}
            allPorts={allPorts}
          />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ExecutorInstancesPage() {
  const [data, setData] = useState<MhxyExecutorInstances | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [viewMode, setViewMode] = useState<"list" | "grid">("list");
  const [tapMode, setTapMode] = useState(false);
  const [gridBroadcast, setGridBroadcast] = useState(false);

  // Modal state
  const [modalPort, setModalPort] = useState<string | null>(null);
  const [modalState, setModalState] = useState<ScreenshotState>("idle");

  // Screenshot cache: port → ScreenshotState
  const [screenshots, setScreenshots] = useState<Record<string, ScreenshotState>>({});
  const [lastRefreshAt, setLastRefreshAt] = useState<Date | null>(null);
  const fetchingRef = useRef<Set<string>>(new Set());

  const load = useCallback(() => {
    return api.mhxyExecutorInstances()
      .then(setData)
      .catch((e: unknown) => setError(String(e)));
  }, []);

  useEffect(() => {
    setLoading(true);
    load().finally(() => setLoading(false));
  }, [load]);

  const instances = data?.instances ?? [];

  const fetchScreenshots = useCallback((insts: typeof instances, force = false) => {
    let anyFetched = false;
    for (const inst of insts) {
      const port = inst.port;
      if (fetchingRef.current.has(port)) continue;
      if (!force) {
        const existing = screenshots[port];
        if (existing && existing !== "error") continue;
      }
      fetchingRef.current.add(port);
      anyFetched = true;
      setScreenshots((prev) => ({ ...prev, [port]: "loading" }));
      api.mhxyExecutorScreenshot(port)
        .then((d) => setScreenshots((prev) => ({ ...prev, [port]: d.image_b64 })))
        .catch(() => setScreenshots((prev) => ({ ...prev, [port]: "error" })))
        .finally(() => fetchingRef.current.delete(port));
    }
    if (anyFetched) setLastRefreshAt(new Date());
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [screenshots]);

  const refreshScreenshots = useCallback(() => {
    fetchingRef.current.clear();
    setScreenshots({});
    setLastRefreshAt(null);
    // run after state flush so "loading" renders immediately
    setTimeout(() => fetchScreenshots(instances, true), 0);
  }, [fetchScreenshots, instances]);

  // Fetch all screenshots when entering any view mode
  useEffect(() => {
    if (instances.length === 0) return;
    fetchScreenshots(instances, false);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode, instances.length]);

  // Keep a stable ref to instances so the interval doesn't need to reset
  const instancesRef = useRef(instances);
  useEffect(() => { instancesRef.current = instances; }, [instances]);

  // Auto-refresh every 15s in grid mode: fetch in background, keep old screenshot on failure
  const autoRefresh = useCallback(() => {
    const insts = instancesRef.current;
    if (insts.length === 0) return;
    setLastRefreshAt(new Date());
    for (const inst of insts) {
      const port = inst.port;
      if (fetchingRef.current.has(port)) continue;
      fetchingRef.current.add(port);
      api.mhxyExecutorScreenshot(port)
        .then((d) => setScreenshots((prev) => ({ ...prev, [port]: d.image_b64 })))
        .catch(() => {})
        .finally(() => fetchingRef.current.delete(port));
    }
  }, []);

  useEffect(() => {
    if (viewMode !== "grid") return;
    const id = setInterval(autoRefresh, 3_000);
    return () => clearInterval(id);
  }, [viewMode, autoRefresh]);

  const openModal = useCallback((port: string) => {
    setModalPort(port);
    const cached = screenshots[port];
    if (cached && cached !== "loading") {
      setModalState(cached);
      return;
    }
    setModalState("loading");
    api.mhxyExecutorScreenshot(port)
      .then((d) => {
        setScreenshots((prev) => ({ ...prev, [port]: d.image_b64 }));
        setModalState(d.image_b64);
      })
      .catch(() => setModalState("error"));
  }, [screenshots]);

  const closeModal = useCallback(() => setModalPort(null), []);

  const refreshModalScreenshot = useCallback((port: string) => {
    return api.mhxyExecutorScreenshot(port)
      .then((d) => {
        setScreenshots((prev) => ({ ...prev, [port]: d.image_b64 }));
        setModalState(d.image_b64);
      })
      .catch(() => {});
  }, []);

  // Group instances
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

  const totalOk = instances.filter((i) => i.healthy === true).length;
  const totalAdb = instances.filter((i) => i.adb === true).length;
  const allPorts = instances.map((i) => i.port);

  return (
    <div>
      {/* Modal overlay */}
      {modalPort && (
        <ScreenshotModal
          port={modalPort}
          state={modalState}
          allPorts={instances.map((i) => i.port)}
          onClose={closeModal}
          onRefreshScreenshot={refreshModalScreenshot}
        />
      )}

      {/* Page header — 视图切换和刷新固定在此行，不随模式变化 */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: "0.75rem", flexWrap: "wrap" }}>
        <Link href="/" style={{ color: "var(--text-muted)", fontSize: 13 }}>← 返回总览</Link>
        <span style={{ color: "var(--border-hi)" }}>|</span>
        <h1 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)", margin: 0 }}>
          Windows Executor 实例详情
        </h1>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center" }}>
          <button
            onClick={() => setViewMode("list")}
            style={{
              padding: "4px 10px",
              borderRadius: "var(--r-sm)",
              border: `1px solid ${viewMode === "list" ? "var(--blue)" : "var(--border-hi)"}`,
              background: viewMode === "list" ? "rgba(99,179,237,0.1)" : "transparent",
              color: viewMode === "list" ? "var(--blue)" : "var(--text-muted)",
              fontSize: 11, fontWeight: 500, cursor: "pointer",
            }}
          >
            列表
          </button>
          <button
            onClick={() => setViewMode("grid")}
            style={{
              padding: "4px 10px",
              borderRadius: "var(--r-sm)",
              border: `1px solid ${viewMode === "grid" ? "var(--blue)" : "var(--border-hi)"}`,
              background: viewMode === "grid" ? "rgba(99,179,237,0.1)" : "transparent",
              color: viewMode === "grid" ? "var(--blue)" : "var(--text-muted)",
              fontSize: 11, fontWeight: 500, cursor: "pointer",
            }}
          >
            截图巡检
          </button>
          <span style={{ width: 1, alignSelf: "stretch", background: "var(--border-hi)", margin: "0 2px" }} />
          <button
            onClick={() => { setLoading(true); refreshScreenshots(); load().finally(() => setLoading(false)); }}
            style={{
              padding: "4px 10px",
              borderRadius: "var(--r-sm)",
              border: "1px solid var(--border-hi)",
              background: "transparent",
              color: "var(--blue)",
              fontSize: 11, fontWeight: 500, cursor: "pointer",
            }}
          >
            刷新
          </button>
        </div>
      </div>

      {/* 截图巡检子工具栏 — 仅 grid 模式下展示，右对齐 */}
      {viewMode === "grid" && (
        <div style={{ display: "flex", gap: 6, alignItems: "center", justifyContent: "flex-end", marginBottom: "1rem" }}>
          <button
            onClick={() => { setTapMode((v) => !v); if (tapMode) setGridBroadcast(false); }}
            style={{
              padding: "4px 10px",
              borderRadius: "var(--r-sm)",
              border: `1px solid ${tapMode ? "var(--green)" : "var(--border-hi)"}`,
              background: tapMode ? "rgba(72,187,120,0.15)" : "transparent",
              color: tapMode ? "var(--green)" : "var(--text-muted)",
              fontSize: 11, fontWeight: 500, cursor: "pointer",
            }}
          >
            📍 点击操作{tapMode ? " ON" : ""}
          </button>
          <button
            onClick={() => setGridBroadcast((v) => !v)}
            style={{
              padding: "4px 10px",
              borderRadius: "var(--r-sm)",
              border: `1px solid ${gridBroadcast ? "var(--amber)" : "var(--border-hi)"}`,
              background: gridBroadcast ? "rgba(246,173,85,0.15)" : "transparent",
              color: gridBroadcast ? "var(--amber)" : "var(--text-muted)",
              fontSize: 11, fontWeight: 500, cursor: "pointer",
            }}
          >
            📡 广播{gridBroadcast ? " ON" : ""}
          </button>
        </div>
      )}

      {data?.app_health_checked_at && (
        <p style={{ color: "var(--text-dim)", fontSize: 12, marginBottom: "1.25rem" }}>
          健康检查时间：{fmtTime(data.app_health_checked_at)}
          {instances.length > 0 && (
            <span style={{ marginLeft: 16 }}>
              <span style={{ color: totalOk === instances.length ? "var(--green)" : "var(--amber)", fontWeight: 600 }}>
                {totalOk}/{instances.length}
              </span>
              <span style={{ marginLeft: 4 }}>实例健康</span>
              <span style={{ marginLeft: 12, color: totalAdb === instances.length ? "var(--green)" : "var(--amber)", fontWeight: 600 }}>
                {totalAdb}/{instances.length}
              </span>
              <span style={{ marginLeft: 4 }}>ADB 正常</span>
            </span>
          )}
          <span style={{ marginLeft: 16, color: "var(--text-dim)" }}>
            {lastRefreshAt
              ? `截图刷新于 ${lastRefreshAt.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
              : "截图加载中…"}
            {viewMode === "grid" && (tapMode
              ? (gridBroadcast ? ` · 广播模式：点击任意截图 → 同步到全部 ${allPorts.length} 个实例` : " · 点击截图操作当前实例，黑边区域打开详情")
              : " · 点击卡片可放大")}
          </span>
        </p>
      )}

      {error && (
        <div style={{
          background: "#1a0a0a", border: "1px solid #3a1a1a", borderRadius: "var(--r)",
          padding: "0.75rem 1rem", color: "var(--red)", fontSize: 12, marginBottom: "1rem",
        }}>
          {error}
        </div>
      )}

      {loading ? (
        <div style={{ color: "var(--text-dim)", fontSize: 13, padding: "2rem 0" }}>加载中…</div>
      ) : instances.length === 0 ? (
        <div className="card" style={{ color: "var(--text-muted)", fontSize: 13, textAlign: "center", padding: "2rem" }}>
          暂无实例数据（executor 可能未上报 app_health）
        </div>
      ) : viewMode === "list" ? (
        // ---- List mode ----
        <div className="card">
          {Array.from(groups.entries())
            .sort(([a], [b]) => a - b)
            .map(([gid, insts]) => (
              <GroupBlock key={gid} groupId={gid} instances={insts} screenshots={screenshots} onScreenshot={openModal} />
            ))}

          {standalone.length > 0 && (
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: "0.5rem" }}>
                <span style={{ color: "var(--text)", fontWeight: 600, fontSize: 13 }}>独立实例</span>
              </div>
              <ColumnHeaders />
              <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
                {standalone.map((inst) => <InstanceRow key={inst.port} inst={inst} screenshot={screenshots[inst.port] ?? "idle"} onScreenshot={openModal} />)}
              </div>
            </div>
          )}
        </div>
      ) : (
        // ---- Grid / screenshot patrol mode ----
        <div>
          {Array.from(groups.entries())
            .sort(([a], [b]) => a - b)
            .map(([gid, insts]) => (
              <GroupBlockGrid
                key={gid}
                groupId={gid}
                instances={insts}
                screenshots={screenshots}
                onCardClick={openModal}
                tapMode={tapMode}
                broadcastMode={gridBroadcast}
                allPorts={allPorts}
              />
            ))}

          {standalone.length > 0 && (
            <div style={{ marginBottom: "1.5rem" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: "0.75rem" }}>
                <span style={{ color: "var(--text)", fontWeight: 600, fontSize: 13 }}>独立实例</span>
              </div>
              <div style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
                gap: "0.75rem",
              }}>
                {standalone.map((inst) => (
                  <ScreenshotCard
                    key={inst.port}
                    inst={inst}
                    screenshot={screenshots[inst.port] ?? "idle"}
                    onClick={() => openModal(inst.port)}
                    tapMode={tapMode}
                    broadcastMode={gridBroadcast}
                    allPorts={allPorts}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

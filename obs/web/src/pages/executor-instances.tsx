import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { api, type MhxyExecutorInstances, type MhxyInstanceDetail } from "@/lib/api";
import { isWebCodecsSupported, StreamPlayer, type StreamPlayerStatus } from "@/lib/h264-stream";
import { useIsMobile } from "@/lib/useIsMobile";
import { useFocusMode } from "@/lib/focus-mode-context";

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
const EXECUTOR_WS_BASE = process.env.NEXT_PUBLIC_EXECUTOR_WS_BASE || "ws://192.168.100.149:8765";
const EXECUTOR_HTTP_BASE = EXECUTOR_WS_BASE.replace(/^ws(s)?:\/\//, "http$1://");

// ws-scrcpy embeds a sidebar toolbar on the RIGHT: width = 3.715rem at browser-default 16px ≈ 59px.
// The .device-view uses justify-content:flex-end, so the (video + toolbar) group is flush-right.
// The .video cell is content-sized and uses place-items:center, so the canvas is vertically centered.
const SCRCPY_TOOLBAR_PX = 59;

function streamVideoBounds(
  overlayW: number,
  overlayH: number,
  deviceW: number,
  deviceH: number,
): { left: number; top: number; width: number; height: number } {
  const maxVW = overlayW - SCRCPY_TOOLBAR_PX;
  const dAspect = deviceW / deviceH;
  let vw: number, vh: number;
  if (dAspect >= maxVW / overlayH) {
    vw = maxVW; vh = maxVW / dAspect;      // width-limited (letterboxed top/bottom)
  } else {
    vh = overlayH; vw = overlayH * dAspect; // height-limited (pillarboxed left/right)
  }
  return {
    left: overlayW - vw - SCRCPY_TOOLBAR_PX, // video starts here (justify-end pushes right)
    top: (overlayH - vh) / 2,               // centered vertically
    width: vw,
    height: vh,
  };
}

function ScreenshotModal({
  port,
  state,
  onClose,
  onRefreshScreenshot,
}: {
  port: string;
  state: ScreenshotState;
  onClose: () => void;
  onRefreshScreenshot: (port: string) => Promise<void>;
}) {
  const [viewMode, setViewMode] = useState<"screenshot" | "stream">("screenshot");
  const [tapStatus, setTapStatus] = useState<BroadcastStatus>(null);
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
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

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
    tapPendingRef.current = true;
    setTapStatus({ pending: true });
    api.mhxyExecutorBatchTap([port], px, py)
      .then((d) => {
        const ok = Object.values(d.results).filter(Boolean).length;
        setTapStatus({ pending: false, ok, fail: 1 - ok, px, py });
        setTimeout(() => onRefreshScreenshot(port), 200);
      })
      .catch(() => {
        setTapStatus({ pending: false, ok: 0, fail: 1, px, py });
      })
      .finally(() => { tapPendingRef.current = false; });
  }, [port, onRefreshScreenshot]);

  const handleStreamMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const { left: vL, top: vT, width: vW, height: vH } = streamVideoBounds(rect.width, rect.height, deviceSize.w, deviceSize.h);
    const rx = e.clientX - rect.left - vL;
    const ry = e.clientY - rect.top - vT;
    if (rx < 0 || rx > vW || ry < 0 || ry > vH) { setHoverXY(null); return; }
    setHoverXY({
      x: Math.round(rx / vW * deviceSize.w),
      y: Math.round(ry / vH * deviceSize.h),
    });
  }, [deviceSize]);

  const handleStreamClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    const { left: vL, top: vT, width: vW, height: vH } = streamVideoBounds(rect.width, rect.height, deviceSize.w, deviceSize.h);
    const rx = e.clientX - rect.left - vL;
    const ry = e.clientY - rect.top - vT;
    if (rx < 0 || rx > vW || ry < 0 || ry > vH) return; // toolbar / black bar — skip
    const px = Math.round(rx / vW * deviceSize.w);
    const py = Math.round(ry / vH * deviceSize.h);
    const pctX = (e.clientX - rect.left) / rect.width * 100;
    const pctY = (e.clientY - rect.top) / rect.height * 100;
    setClickRipple({ pctX, pctY, key: Date.now() });
    if (tapPendingRef.current) return;
    tapPendingRef.current = true;
    setTapStatus({ pending: true });
    api.mhxyExecutorBatchTap([port], px, py)
      .then((d) => {
        const ok = Object.values(d.results).filter(Boolean).length;
        setTapStatus({ pending: false, ok, fail: 1 - ok, px, py });
      })
      .catch(() => {
        setTapStatus({ pending: false, ok: 0, fail: 1, px, py });
      })
      .finally(() => { tapPendingRef.current = false; });
  }, [deviceSize, port]);

  const isImage = state !== "loading" && state !== "error" && state !== "idle";

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
              onClick={() => setViewMode(m)}
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
          {viewMode === "screenshot"
            ? " — 点击截图操作当前实例，点击外侧或 Esc 关闭"
            : ` — 点击操作当前实例（${deviceSize.w}×${deviceSize.h}），点击外侧关闭`}
        </span>
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
        {tapStatus ? (tapStatus.pending ? (
          <span style={{ color: "var(--text-dim)" }}>点击中…</span>
        ) : (
          <span>
            <span style={{ color: tapStatus.fail === 0 ? "var(--green)" : "var(--red)" }}>
              {tapStatus.fail === 0 ? "✓ 成功" : "✗ 失败"}
            </span>
            <span style={{ color: "var(--text-dim)", marginLeft: 12 }}>
              @ ({tapStatus.px}, {tapStatus.py})
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
        {ROLE_LABEL[inst.role] ?? (inst.role || "—")}
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

function InstanceRowMobile({
  inst,
  screenshot,
  onScreenshot,
}: {
  inst: MhxyInstanceDetail;
  screenshot: ScreenshotState;
  onScreenshot: (port: string) => void;
}) {
  return (
    <div style={{
      display: "flex",
      gap: "0.75rem",
      padding: "0.6rem 0.75rem",
      borderRadius: "var(--r-sm)",
      background: "var(--bg2)",
      border: "1px solid var(--border)",
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
          <span style={{ color: "var(--text)", fontFamily: "var(--font-mono)", fontWeight: 600 }}>
            {inst.port}
          </span>
          <span style={{ color: "var(--text)" }}>{inst.school || "—"}</span>
          <span style={{ color: ROLE_COLOR[inst.role] ?? "var(--text-muted)", fontSize: 11 }}>
            {ROLE_LABEL[inst.role] ?? (inst.role || "—")}
          </span>
        </div>
        <div style={{ display: "flex", gap: 12, fontSize: 11, color: "var(--text-dim)", flexWrap: "wrap" }}>
          <span>ADB <CheckIcon ok={inst.adb} /></span>
          <span>截图 <CheckIcon ok={inst.screenshot} /></span>
          <span>OCR <CheckIcon ok={inst.ocr} /></span>
          {inst.latency_ms !== null && inst.latency_ms !== undefined && (
            <span style={{ fontFamily: "var(--font-mono)" }}>{inst.latency_ms} ms</span>
          )}
        </div>
        <div style={{
          marginTop: 4,
          fontSize: 11,
          color: inst.error ? "var(--red)" : inst.healthy === true ? "var(--green)" : inst.healthy === false ? "var(--red)" : "var(--text-muted)",
          wordBreak: "break-word",
        }}>
          {inst.error || (inst.healthy === true ? "正常" : inst.healthy === false ? "异常" : "未知")}
        </div>
      </div>
      <div
        title="点击放大查看截图"
        onClick={() => onScreenshot(inst.port)}
        style={{
          width: 96,
          aspectRatio: "16/9",
          background: "#0d0d0d",
          borderRadius: "var(--r-sm)",
          overflow: "hidden",
          cursor: "pointer",
          border: "1px solid var(--border)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}
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
  isMobile,
}: {
  groupId: number;
  instances: MhxyInstanceDetail[];
  screenshots: Record<string, ScreenshotState>;
  onScreenshot: (port: string) => void;
  isMobile: boolean;
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
      {!isMobile && <ColumnHeaders />}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
        {leader && (isMobile
          ? <InstanceRowMobile inst={leader} screenshot={screenshots[leader.port] ?? "idle"} onScreenshot={onScreenshot} />
          : <InstanceRow inst={leader} screenshot={screenshots[leader.port] ?? "idle"} onScreenshot={onScreenshot} />
        )}
        {members.map((m) => isMobile
          ? <InstanceRowMobile key={m.port} inst={m} screenshot={screenshots[m.port] ?? "idle"} onScreenshot={onScreenshot} />
          : <InstanceRow key={m.port} inst={m} screenshot={screenshots[m.port] ?? "idle"} onScreenshot={onScreenshot} />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Grid view components
// ---------------------------------------------------------------------------

// Returns the pixel bounds of the actual image content inside an objectFit:contain img element.
function getContainBoundsGeneric(elem: HTMLElement, naturalWidth: number, naturalHeight: number) {
  const rect = elem.getBoundingClientRect();
  const imgAspect = naturalWidth / naturalHeight;
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

function getContainBounds(img: HTMLImageElement) {
  return getContainBoundsGeneric(img, img.naturalWidth, img.naturalHeight);
}

function getVideoContentBounds(
  videoW: number,
  videoH: number,
  deviceW: number,
  deviceH: number,
): { left: number; top: number; width: number; height: number } {
  const deviceAspect = deviceW / deviceH;
  const videoAspect = videoW / videoH;
  if (deviceAspect > videoAspect) {
    const height = videoW / deviceAspect;
    return { left: 0, top: (videoH - height) / 2, width: videoW, height };
  }
  const width = videoH * deviceAspect;
  return { left: (videoW - width) / 2, top: 0, width, height: videoH };
}

type BroadcastScope = "single" | "all" | "leaders" | "custom";
type NativeStreamQuality = "low" | "medium" | "high";

const NATIVE_STREAM_QUALITY_LABEL: Record<NativeStreamQuality, string> = {
  low: "低",
  medium: "中",
  high: "高",
};

// Approximate bitrate per quality tier (matches executor STREAM_QUALITY_PRESETS defaults).
const NATIVE_STREAM_BITRATE: Record<NativeStreamQuality, number> = {
  low: 200_000,
  medium: 400_000,
  high: 1_500_000,
};

const TONE_STYLE = {
  blue: {
    color: "var(--blue)",
    bg: "rgba(96,165,250,0.12)",
    border: "rgba(96,165,250,0.38)",
  },
  green: {
    color: "var(--green)",
    bg: "rgba(52,211,153,0.12)",
    border: "rgba(52,211,153,0.38)",
  },
  amber: {
    color: "var(--amber)",
    bg: "rgba(251,191,36,0.12)",
    border: "rgba(251,191,36,0.38)",
  },
  teal: {
    color: "var(--teal)",
    bg: "rgba(45,212,191,0.12)",
    border: "rgba(45,212,191,0.38)",
  },
} as const;

type Tone = keyof typeof TONE_STYLE;

function ToolbarSection({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{
      display: "inline-flex",
      alignItems: "center",
      gap: 6,
      minHeight: 30,
      padding: "2px 4px",
      borderRadius: "var(--r-sm)",
    }}>
      <span style={{
        color: "var(--text-dim)",
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: "0.06em",
      }}>
        {label}
      </span>
      {children}
    </div>
  );
}

function ToolbarButton({
  active,
  tone = "blue",
  disabled = false,
  title,
  onClick,
  children,
}: {
  active: boolean;
  tone?: Tone;
  disabled?: boolean;
  title?: string;
  onClick: () => void;
  children: ReactNode;
}) {
  const selected = TONE_STYLE[tone];
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        height: 28,
        padding: "0 10px",
        borderRadius: "var(--r-sm)",
        border: `1px solid ${active ? selected.border : "transparent"}`,
        background: active ? selected.bg : "transparent",
        color: active ? selected.color : "var(--text-muted)",
        fontSize: 11,
        fontWeight: 600,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.42 : 1,
        transition: "background var(--dur) var(--ease), border-color var(--dur) var(--ease), color var(--dur) var(--ease)",
      }}
    >
      {children}
    </button>
  );
}

function ToolbarDivider() {
  return <span style={{ width: 1, alignSelf: "stretch", minHeight: 26, background: "var(--border)" }} />;
}

// Estimated average screenshot JPEG payload per fetch (bytes).
// screencap -p → JPEG quality=85 at 1600×900, typical game scene ~200 KB.
const SCREENSHOT_AVG_BYTES = 200 * 1024;
const SCREENSHOT_INTERVAL_MS = 3000;

function estimateBandwidthBps({
  streamingPorts,
  streamingQuality,
  pollingPortCount,
  bitrateOverride = NATIVE_STREAM_BITRATE,
}: {
  streamingPorts: number;
  streamingQuality: NativeStreamQuality;
  pollingPortCount: number;
  bitrateOverride?: Record<NativeStreamQuality, number>;
}): { totalKbps: number; streamKbps: number; screenshotKbps: number } {
  const streamKbps = (streamingPorts * bitrateOverride[streamingQuality]) / 1000;
  const screenshotKbps = (pollingPortCount * SCREENSHOT_AVG_BYTES * 8) / (SCREENSHOT_INTERVAL_MS / 1000) / 1000;
  return { totalKbps: Math.round(streamKbps + screenshotKbps), streamKbps: Math.round(streamKbps), screenshotKbps: Math.round(screenshotKbps) };
}

function BandwidthBadge({
  streamingPorts,
  nativeStreamQuality,
  pollingPortCount,
  actualBitrate,
}: {
  streamingPorts: number;
  nativeStreamQuality: NativeStreamQuality;
  pollingPortCount: number;
  actualBitrate: Partial<Record<NativeStreamQuality, number>>;
}) {
  const bitrateOverride = { ...NATIVE_STREAM_BITRATE, ...actualBitrate };
  const est = estimateBandwidthBps({ streamingPorts, streamingQuality: nativeStreamQuality, pollingPortCount, bitrateOverride });
  const label = est.totalKbps >= 1000
    ? `${(est.totalKbps / 1000).toFixed(1)} Mbps`
    : `~${est.totalKbps} kbps`;
  const tooltipLines = [
    actualBitrate[nativeStreamQuality] ? "实测下行带宽（从 NAS 到浏览器）" : "预估下行带宽（从 NAS 到浏览器）",
    "",
    `H264 推流：${streamingPorts} × ${NATIVE_STREAM_QUALITY_LABEL[nativeStreamQuality]}档 → ${est.streamKbps} kbps${actualBitrate[nativeStreamQuality] ? " (实测)" : " (估算)"}`,
    `截图轮询：${pollingPortCount} 端口 / ${SCREENSHOT_INTERVAL_MS / 1000}s × ~${Math.round(SCREENSHOT_AVG_BYTES / 1024)}KB → ${est.screenshotKbps} kbps`,
  ];
  return (
    <span
      title={tooltipLines.join("\n")}
      style={{
        display: "inline-flex", alignItems: "center", gap: 6,
        height: 28,
        padding: "0 9px", borderRadius: "var(--r-sm)",
        background: est.totalKbps > 2000 ? "rgba(251,191,36,0.10)" : "rgba(52,211,153,0.10)",
        border: `1px solid ${est.totalKbps > 2000 ? "rgba(251,191,36,0.32)" : "rgba(52,211,153,0.32)"}`,
        fontSize: 11, fontWeight: 650,
        color: est.totalKbps > 2000 ? "var(--amber)" : "var(--green)",
        cursor: "default",
      }}
    >
      <span style={{ color: "var(--text-dim)", fontWeight: 700, letterSpacing: "0.04em" }}>BW</span>
      <span>{label}</span>
    </span>
  );
}

function ScreenshotCard({
  inst,
  screenshot,
  onClick,
  tapMode = false,
  swipeEnabled = false,
  broadcastScope = "single",
  allPorts = [],
  leaderPorts = [],
  nativeStreamQuality,
  onStreamModeChange,
  isSelected = false,
  onToggleSelect,
  showSelectionUI = false,
}: {
  inst: MhxyInstanceDetail;
  screenshot: ScreenshotState;
  onClick: () => void;
  tapMode?: boolean;
  broadcastScope?: BroadcastScope;
  allPorts?: string[];
  leaderPorts?: string[];
  nativeStreamQuality: NativeStreamQuality;
  onStreamModeChange?: (port: string, streaming: boolean) => void;
  isSelected?: boolean;
  onToggleSelect?: () => void;
  showSelectionUI?: boolean;
  swipeEnabled?: boolean;
}) {
  const [tapStatus, setTapStatus] = useState<{ pending: boolean; ok?: number; fail?: number; kind?: "tap" | "swipe" } | null>(null);
  const tapPendingRef = useRef(false);
  const [ripple, setRipple] = useState<{ pctX: number; pctY: number; key: number } | null>(null);
  const [hoverCoord, setHoverCoord] = useState<{ x: number; y: number } | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const swipeLineRef = useRef<HTMLDivElement>(null);
  const swipeStartDotRef = useRef<HTMLDivElement>(null);
  const swipeEndDotRef = useRef<HTMLDivElement>(null);
  const swipeRafRef = useRef<number | null>(null);
  const dragStartRef = useRef<{
    px: number;
    py: number;
    clientX: number;
    clientY: number;
    localX: number;
    localY: number;
    pctX: number;
    pctY: number;
  } | null>(null);
  const [streamMode, setStreamMode] = useState(false);
  const [nativeStreamMode, setNativeStreamMode] = useState(true);
  const [nativeStreamStatus, setNativeStreamStatus] = useState<StreamPlayerStatus>("closed");
  const [nativeStreamDetail, setNativeStreamDetail] = useState("");
  // Physical device resolution; updated when a screenshot arrives.
  // Default matches executor W=1600 H=900 so tap coords are correct even before first screenshot.
  const [deviceSize, setDeviceSize] = useState<{ w: number; h: number }>({ w: 1600, h: 900 });
  const imgRef = useRef<HTMLImageElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const playerRef = useRef<StreamPlayer | null>(null);
  const streamModeRef = useRef(streamMode);
  const nativeStreamModeRef = useRef(nativeStreamMode);
  streamModeRef.current = streamMode;
  nativeStreamModeRef.current = nativeStreamMode;

  const isImageLoaded = screenshot !== "idle" && screenshot !== "loading" && screenshot !== "error";
  const adbDevice = `emulator-${parseInt(inst.port) - 1}`;
  const streamUrl = `${WS_SCRCPY_BASE}/embed.html?device=${adbDevice}`;
  const nativeStreamUrl = `${EXECUTOR_WS_BASE}/ws/stream/${inst.port}?quality=${nativeStreamQuality}`;

  // Refine device resolution from the latest screenshot when one arrives.
  useEffect(() => {
    if (!isImageLoaded || typeof screenshot !== "string") return;
    const img = new Image();
    img.onload = () => setDeviceSize({ w: img.naturalWidth, h: img.naturalHeight });
    img.src = `data:image/jpeg;base64,${screenshot}`;
  }, [isImageLoaded, screenshot]);

  // Notify parent on toggle so it can skip polling; auto-clear on unmount.
  const toggleStream = useCallback(() => {
    setStreamMode((prev) => {
      const next = !prev;
      if (next) {
        nativeStreamModeRef.current = false;
        setNativeStreamMode(false);
      }
      onStreamModeChange?.(inst.port, next || nativeStreamModeRef.current);
      return next;
    });
  }, [inst.port, onStreamModeChange]);

  const toggleNativeStream = useCallback(() => {
    setNativeStreamMode((prev) => {
      const next = !prev;
      if (next) {
        streamModeRef.current = false;
        setStreamMode(false);
      }
      onStreamModeChange?.(inst.port, next || streamModeRef.current);
      return next;
    });
  }, [inst.port, onStreamModeChange]);

  useEffect(() => {
    if (!nativeStreamMode || !canvasRef.current) return;
    if (!isWebCodecsSupported()) {
      setNativeStreamStatus("error");
      return;
    }
    const player = new StreamPlayer({
      url: nativeStreamUrl,
      canvas: canvasRef.current,
      onStatus: (status, detail) => {
        setNativeStreamStatus(status);
        setNativeStreamDetail(detail ?? "");
      },
      onResolution: () => {},
    });
    playerRef.current = player;
    return () => {
      player.close();
      playerRef.current = null;
    };
  }, [nativeStreamMode, nativeStreamUrl]);

  useEffect(() => {
    onStreamModeChange?.(inst.port, nativeStreamModeRef.current || streamModeRef.current);
    return () => {
      if (streamModeRef.current || nativeStreamModeRef.current) onStreamModeChange?.(inst.port, false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const currentTargets = useCallback(() => {
    return broadcastScope === "leaders" ? leaderPorts
      : broadcastScope === "all" ? allPorts
      : [inst.port];
  }, [broadcastScope, allPorts, leaderPorts, inst.port]);

  const performTap = useCallback((px: number, py: number, containerRect: DOMRect, clientX: number, clientY: number) => {
    setRipple({
      pctX: (clientX - containerRect.left) / containerRect.width * 100,
      pctY: (clientY - containerRect.top) / containerRect.height * 100,
      key: Date.now(),
    });
    if (tapPendingRef.current) return;
    const targets = currentTargets();
    if (targets.length === 0) return;
    tapPendingRef.current = true;
    setTapStatus({ pending: true, kind: "tap" });
    api.mhxyExecutorBatchTap(targets, px, py)
      .then((d) => {
        const ok = Object.values(d.results).filter(Boolean).length;
        setTapStatus({ pending: false, ok, fail: targets.length - ok, kind: "tap" });
        setTimeout(() => setTapStatus(null), 2500);
      })
      .catch(() => {
        setTapStatus({ pending: false, ok: 0, fail: targets.length, kind: "tap" });
        setTimeout(() => setTapStatus(null), 2500);
      })
      .finally(() => { tapPendingRef.current = false; });
  }, [currentTargets]);

  const performSwipe = useCallback((x1: number, y1: number, x2: number, y2: number, durationMs = 300) => {
    if (tapPendingRef.current) return;
    const targets = currentTargets();
    if (targets.length === 0) return;
    tapPendingRef.current = true;
    setTapStatus({ pending: true, kind: "swipe" });
    api.mhxyExecutorBatchSwipe(targets, x1, y1, x2, y2, durationMs)
      .then((d) => {
        const ok = Object.values(d.results).filter(Boolean).length;
        setTapStatus({ pending: false, ok, fail: targets.length - ok, kind: "swipe" });
        setTimeout(() => setTapStatus(null), 2500);
      })
      .catch(() => {
        setTapStatus({ pending: false, ok: 0, fail: targets.length, kind: "swipe" });
        setTimeout(() => setTapStatus(null), 2500);
      })
      .finally(() => { tapPendingRef.current = false; });
  }, [currentTargets]);

  const pointFromClient = useCallback((elem: HTMLDivElement, clientX: number, clientY: number) => {
    if (!tapMode) return;
    const elemRect = elem.getBoundingClientRect();
    if (nativeStreamMode) {
      if (!canvasRef.current || canvasRef.current.width === 0 || canvasRef.current.height === 0) return;
      const videoW = canvasRef.current.width;
      const videoH = canvasRef.current.height;
      const { cx, cy, cw, ch } = getContainBoundsGeneric(
        canvasRef.current,
        videoW,
        videoH,
      );
      const frameX = (clientX - cx) / cw * videoW;
      const frameY = (clientY - cy) / ch * videoH;
      if (frameX < 0 || frameX > videoW || frameY < 0 || frameY > videoH) return null;
      const content = getVideoContentBounds(videoW, videoH, deviceSize.w, deviceSize.h);
      const rx = frameX - content.left;
      const ry = frameY - content.top;
      if (rx < 0 || rx > content.width || ry < 0 || ry > content.height) return null;
      return {
        x: Math.round(rx / content.width * deviceSize.w),
        y: Math.round(ry / content.height * deviceSize.h),
        localX: clientX - elemRect.left,
        localY: clientY - elemRect.top,
        pctX: (clientX - elemRect.left) / elemRect.width * 100,
        pctY: (clientY - elemRect.top) / elemRect.height * 100,
      };
    }
    if (streamMode) {
      const { left, top, width, height } = streamVideoBounds(elemRect.width, elemRect.height, deviceSize.w, deviceSize.h);
      const rx = clientX - elemRect.left - left;
      const ry = clientY - elemRect.top - top;
      if (rx < 0 || rx > width || ry < 0 || ry > height) return null;
      return {
        x: Math.round(rx / width * deviceSize.w),
        y: Math.round(ry / height * deviceSize.h),
        localX: clientX - elemRect.left,
        localY: clientY - elemRect.top,
        pctX: (clientX - elemRect.left) / elemRect.width * 100,
        pctY: (clientY - elemRect.top) / elemRect.height * 100,
      };
    }
    if (!imgRef.current || !isImageLoaded) return null;
    const { cx, cy, cw, ch } = getContainBounds(imgRef.current);
    const rx = clientX - cx;
    const ry = clientY - cy;
    if (rx < 0 || rx > cw || ry < 0 || ry > ch) return null;
    return {
      x: Math.round(rx / cw * imgRef.current.naturalWidth),
      y: Math.round(ry / ch * imgRef.current.naturalHeight),
      localX: clientX - elemRect.left,
      localY: clientY - elemRect.top,
      pctX: (clientX - elemRect.left) / elemRect.width * 100,
      pctY: (clientY - elemRect.top) / elemRect.height * 100,
    };
  }, [tapMode, nativeStreamMode, streamMode, deviceSize, isImageLoaded]);

  const updateSwipePreview = useCallback((fromX: number, fromY: number, toX: number, toY: number) => {
    if (swipeRafRef.current !== null) {
      cancelAnimationFrame(swipeRafRef.current);
    }
    swipeRafRef.current = requestAnimationFrame(() => {
      swipeRafRef.current = null;
      const line = swipeLineRef.current;
      const startDot = swipeStartDotRef.current;
      const endDot = swipeEndDotRef.current;
      if (!line || !startDot || !endDot) return;
      const dx = toX - fromX;
      const dy = toY - fromY;
      const length = Math.hypot(dx, dy);
      line.style.display = length >= 3 ? "block" : "none";
      line.style.width = `${length}px`;
      line.style.transform = `translate(${fromX}px, ${fromY}px) rotate(${Math.atan2(dy, dx)}rad)`;
      startDot.style.display = "block";
      endDot.style.display = "block";
      startDot.style.transform = `translate(${fromX - 4}px, ${fromY - 4}px)`;
      endDot.style.transform = `translate(${toX - 4}px, ${toY - 4}px)`;
    });
  }, []);

  const hideSwipePreview = useCallback(() => {
    if (swipeRafRef.current !== null) {
      cancelAnimationFrame(swipeRafRef.current);
      swipeRafRef.current = null;
    }
    if (swipeLineRef.current) swipeLineRef.current.style.display = "none";
    if (swipeStartDotRef.current) swipeStartDotRef.current.style.display = "none";
    if (swipeEndDotRef.current) swipeEndDotRef.current.style.display = "none";
  }, []);

  useEffect(() => () => hideSwipePreview(), [hideSwipePreview]);

  const handleImgAreaPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!tapMode) return;
    const point = pointFromClient(e.currentTarget, e.clientX, e.clientY);
    if (!point) {
      setHoverCoord(null);
      return;
    }
    setHoverCoord({ x: point.x, y: point.y });
    if (swipeEnabled && dragStartRef.current) {
      updateSwipePreview(dragStartRef.current.localX, dragStartRef.current.localY, point.localX, point.localY);
    }
  }, [tapMode, swipeEnabled, pointFromClient, updateSwipePreview]);


  const handleImgAreaPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!tapMode) return;
    const point = pointFromClient(e.currentTarget, e.clientX, e.clientY);
    if (!point) return;
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragStartRef.current = {
      px: point.x,
      py: point.y,
      clientX: e.clientX,
      clientY: e.clientY,
      localX: point.localX,
      localY: point.localY,
      pctX: point.pctX,
      pctY: point.pctY,
    };
    setIsDragging(true);
    updateSwipePreview(point.localX, point.localY, point.localX, point.localY);
  }, [tapMode, pointFromClient, updateSwipePreview]);

  const handleImgAreaPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!tapMode) return;
    const start = dragStartRef.current;
    dragStartRef.current = null;
    setIsDragging(false);
    hideSwipePreview();
    if (!start) return;
    const point = pointFromClient(e.currentTarget, e.clientX, e.clientY);
    if (!point) return;
    e.stopPropagation();
    const dx = point.x - start.px;
    const dy = point.y - start.py;
    const clientDistance = Math.hypot(e.clientX - start.clientX, e.clientY - start.clientY);
    const containerRect = e.currentTarget.getBoundingClientRect();
    if (clientDistance <= 10) {
      performTap(start.px, start.py, containerRect, start.clientX, start.clientY);
      return;
    }
    if (!swipeEnabled) return;
    if (Math.hypot(dx, dy) < 24) return;
    performSwipe(start.px, start.py, point.x, point.y, 300);
  }, [tapMode, swipeEnabled, pointFromClient, performTap, performSwipe, hideSwipePreview]);

  const clearDragState = useCallback(() => {
    dragStartRef.current = null;
    setIsDragging(false);
    setHoverCoord(null);
    hideSwipePreview();
  }, [hideSwipePreview]);

  const statusColor = inst.healthy === true
    ? "var(--green)"
    : inst.healthy === false
    ? "var(--red)"
    : "var(--text-muted)";

  return (
    <div
      onClick={onClick}
      style={{
        border: showSelectionUI && isSelected
          ? "1.5px solid var(--blue)"
          : "1px solid var(--border)",
        borderRadius: "var(--r-sm)",
        overflow: "hidden",
        cursor: "pointer",
        background: showSelectionUI && isSelected
          ? "rgba(99,179,237,0.04)"
          : "var(--bg2)",
        transition: "border-color 0.15s",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = showSelectionUI && isSelected ? "var(--blue)" : "var(--blue)")}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = showSelectionUI && isSelected ? "var(--blue)" : "var(--border)")}
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
        {showSelectionUI && onToggleSelect && (
          <div
            onClick={(e) => { e.stopPropagation(); onToggleSelect(); }}
            title={isSelected ? "取消选中" : "选中此实例"}
            style={{
              width: 16, height: 16, borderRadius: 3,
              border: isSelected ? "1.5px solid var(--blue)" : "1.5px solid var(--border-hi)",
              background: isSelected ? "rgba(99,179,237,0.2)" : "transparent",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              fontSize: 10, lineHeight: 1, cursor: "pointer",
              flexShrink: 0,
            }}
          >
            {isSelected && <span style={{ color: "var(--blue)", fontWeight: 700 }}>✓</span>}
          </div>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); toggleStream(); }}
          title={streamMode ? "切回截图轮询" : "切换到 ws-scrcpy 实时流"}
          style={{
            marginLeft: "auto",
            padding: "1px 6px",
            borderRadius: "var(--r-sm)",
            border: `1px solid ${streamMode ? "var(--blue)" : "var(--border-hi)"}`,
            background: streamMode ? "rgba(99,179,237,0.15)" : "transparent",
            color: streamMode ? "var(--blue)" : "var(--text-muted)",
            fontSize: 10, lineHeight: 1.2, cursor: "pointer",
          }}
        >
          📺{streamMode ? " ON" : ""}
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); toggleNativeStream(); }}
          title={nativeStreamMode ? `关闭自建 H.264 实时流（${NATIVE_STREAM_QUALITY_LABEL[nativeStreamQuality]}画质）` : `切换到自建 H.264 实时流（${NATIVE_STREAM_QUALITY_LABEL[nativeStreamQuality]}画质）`}
          style={{
            padding: "1px 6px",
            borderRadius: "var(--r-sm)",
            border: `1px solid ${nativeStreamMode ? "var(--teal)" : "var(--border-hi)"}`,
            background: nativeStreamMode ? "rgba(56,178,172,0.15)" : "transparent",
            color: nativeStreamMode ? "var(--teal)" : "var(--text-muted)",
            fontSize: 10, lineHeight: 1.2, cursor: "pointer",
          }}
        >
          H264 {NATIVE_STREAM_QUALITY_LABEL[nativeStreamQuality]}{nativeStreamMode ? " ON" : ""}
        </button>
        <span style={{
          width: 7, height: 7, borderRadius: 999,
          background: statusColor, flexShrink: 0,
        }} />
      </div>

      {/* screenshot area — fixed aspect ratio 16:9 */}
      <div
        style={{
          background: "#0d0d0d",
          aspectRatio: "16/9",
          display: "flex", alignItems: "center", justifyContent: "center",
          position: "relative",
          cursor: tapMode && (nativeStreamMode || streamMode || isImageLoaded)
            ? isDragging ? "grabbing" : "crosshair"
            : undefined,
          touchAction: tapMode ? "pan-y" : undefined,
        }}
        onPointerMove={handleImgAreaPointerMove}
        onMouseLeave={clearDragState}
        onPointerDown={handleImgAreaPointerDown}
        onPointerUp={handleImgAreaPointerUp}
        onPointerCancel={clearDragState}
        onClick={(e) => { if (tapMode) e.stopPropagation(); }}
      >
        {nativeStreamMode ? (
          <>
            <canvas
              ref={canvasRef}
              style={{
                width: "100%", height: "100%",
                objectFit: "contain",
                display: "block",
              }}
            />
            {nativeStreamStatus !== "playing" && (
              <div style={{
                position: "absolute", inset: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
                background: "rgba(0,0,0,0.55)",
                color: nativeStreamStatus === "error" ? "var(--red)" : "var(--text-dim)",
                fontSize: 11,
                pointerEvents: "none",
                padding: 8,
                textAlign: "center",
              }}>
                {nativeStreamStatus === "connecting" && "连接中…"}
                {nativeStreamStatus === "waiting-keyframe" && "等待关键帧…"}
                {nativeStreamStatus === "restarting" && "重连中…"}
                {nativeStreamStatus === "error" && (
                  isWebCodecsSupported()
                    ? `自建流解码失败${nativeStreamDetail ? `：${nativeStreamDetail.slice(0, 80)}` : ""}`
                    : "浏览器不支持 WebCodecs"
                )}
                {nativeStreamStatus === "closed" && "自建流已断开"}
              </div>
            )}
          </>
        ) : streamMode ? (
          <iframe
            src={streamUrl}
            style={{
              width: "100%", height: "100%",
              border: 0, display: "block",
              pointerEvents: "none",
            }}
            allow="autoplay"
          />
        ) : (
          <>
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
          </>
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
        <div
          ref={swipeLineRef}
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            display: "none",
            width: 0,
            height: 2,
            background: "var(--amber)",
            transformOrigin: "0 50%",
            pointerEvents: "none",
            boxShadow: "0 0 0 1px rgba(0,0,0,0.55), 0 0 10px rgba(251,191,36,0.35)",
            zIndex: 3,
          }}
        />
        <div
          ref={swipeStartDotRef}
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            display: "none",
            width: 8,
            height: 8,
            borderRadius: 999,
            background: "var(--amber)",
            border: "1px solid rgba(0,0,0,0.65)",
            pointerEvents: "none",
            zIndex: 4,
          }}
        />
        <div
          ref={swipeEndDotRef}
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            display: "none",
            width: 8,
            height: 8,
            borderRadius: 999,
            background: "var(--green)",
            border: "1px solid rgba(0,0,0,0.65)",
            pointerEvents: "none",
            zIndex: 4,
          }}
        />
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
              ? (broadcastScope === "leaders" ? `${tapStatus.kind === "swipe" ? "滑动队长" : "队长"} ${leaderPorts.length}…`
                : broadcastScope === "all" ? `${tapStatus.kind === "swipe" ? "滑动广播" : "广播"} ${allPorts.length}…`
                : tapStatus.kind === "swipe" ? "滑动…" : "…")
              : `${tapStatus.kind === "swipe" ? "滑" : "点"} ✓${tapStatus.ok}${tapStatus.fail ? ` ✗${tapStatus.fail}` : ""}`}
          </div>
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
  broadcastScope,
  allPorts,
  leaderPorts,
  nativeStreamQuality,
  onStreamModeChange,
  selectedPorts,
  togglePortSelection,
  customSelectionActive,
  gridColumns,
  swipeEnabled,
}: {
  groupId: number;
  instances: MhxyInstanceDetail[];
  screenshots: Record<string, ScreenshotState>;
  onCardClick: (port: string) => void;
  tapMode: boolean;
  broadcastScope: BroadcastScope;
  allPorts: string[];
  leaderPorts: string[];
  nativeStreamQuality: NativeStreamQuality;
  onStreamModeChange: (port: string, streaming: boolean) => void;
  selectedPorts: Set<string>;
  togglePortSelection: (port: string) => void;
  customSelectionActive: boolean;
  gridColumns: 1 | 2 | 3 | 4;
  swipeEnabled: boolean;
}) {
  const leader = instances.find((i) => i.role === "leader");
  const members = instances.filter((i) => i.role !== "leader");
  const allOk = instances.every((i) => i.healthy === true);
  const anyFail = instances.some((i) => i.healthy === false);
  const badgeColor = allOk ? "var(--green)" : anyFail ? "var(--red)" : "var(--amber)";
  const ordered = broadcastScope === "leaders"
    ? (leader ? [leader] : [])
    : broadcastScope === "custom" && customSelectionActive
    ? instances.filter((i) => selectedPorts.has(i.port))
    : (leader ? [leader, ...members] : members);

  if (broadcastScope === "leaders" && !leader) return null;

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
        gridTemplateColumns: `repeat(${gridColumns}, minmax(0, 1fr))`,
        gap: "0.75rem",
      }}>
        {ordered.map((inst) => (
          <ScreenshotCard
            key={inst.port}
            inst={inst}
            screenshot={screenshots[inst.port] ?? "idle"}
            onClick={() => onCardClick(inst.port)}
            tapMode={tapMode}
            broadcastScope={broadcastScope}
            allPorts={allPorts}
            leaderPorts={leaderPorts}
            nativeStreamQuality={nativeStreamQuality}
            onStreamModeChange={onStreamModeChange}
            isSelected={selectedPorts.has(inst.port)}
            onToggleSelect={() => togglePortSelection(inst.port)}
            showSelectionUI={broadcastScope === "custom" && !customSelectionActive}
            swipeEnabled={swipeEnabled}
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
  const isMobile = useIsMobile();
  const [data, setData] = useState<MhxyExecutorInstances | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [viewMode, setViewMode] = useState<"list" | "grid">("grid");
  const [tapMode, setTapMode] = useState(true);
  const [swipeEnabled, setSwipeEnabled] = useState(false);
  const { focus: focusMode, setFocus: setFocusMode } = useFocusMode();
  const [broadcastScope, setBroadcastScope] = useState<BroadcastScope>("all");
  const [selectedPorts, setSelectedPorts] = useState<Set<string>>(new Set());
  const [customSelectionActive, setCustomSelectionActive] = useState(false);
  const togglePortSelection = useCallback((port: string) => {
    setSelectedPorts((prev) => {
      const next = new Set(prev);
      if (next.has(port)) next.delete(port); else next.add(port);
      return next;
    });
  }, []);
  const [nativeStreamQuality, setNativeStreamQuality] = useState<NativeStreamQuality>("low");
  const [gridColumns, setGridColumns] = useState<1 | 2 | 3 | 4>(3);

  // Modal state
  const [modalPort, setModalPort] = useState<string | null>(null);
  const [modalState, setModalState] = useState<ScreenshotState>("idle");

  // Screenshot cache: port → ScreenshotState
  const [screenshots, setScreenshots] = useState<Record<string, ScreenshotState>>({});
  const fetchingRef = useRef<Set<string>>(new Set());

  // Actual measured bitrate per quality tier, fetched from executor /stream/stats.
  const [actualBitrate, setActualBitrate] = useState<Partial<Record<NativeStreamQuality, number>>>({});

  useEffect(() => {
    const fetchStats = () => {
      fetch(`${EXECUTOR_HTTP_BASE}/stream/stats`)
        .then((r) => r.ok ? r.json() : null)
        .then((d) => d && setActualBitrate(d))
        .catch(() => {});
    };
    fetchStats();
    const id = setInterval(fetchStats, 10_000);
    return () => clearInterval(id);
  }, []);

  // Ports currently rendered as ws-scrcpy live stream — these skip screenshot polling.
  const streamingPortsRef = useRef<Set<string>>(new Set());
  // Mirror of streamingPortsRef as state for BandwidthBadge re-renders.
  const [streamingPortCount, setStreamingPortCount] = useState(0);
  const setPortStreaming = useCallback((port: string, streaming: boolean) => {
    if (streaming) streamingPortsRef.current.add(port);
    else streamingPortsRef.current.delete(port);
    setStreamingPortCount(streamingPortsRef.current.size);
  }, []);

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
      if (streamingPortsRef.current.has(port)) continue;
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
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [screenshots]);

  const refreshScreenshots = useCallback(() => {
    fetchingRef.current.clear();
    setScreenshots({});
    // run after state flush so "loading" renders immediately
    setTimeout(() => fetchScreenshots(instances, true), 0);
  }, [fetchScreenshots, instances]);

  // Fetch all screenshots when entering any view mode
  useEffect(() => {
    if (instances.length === 0) return;
    fetchScreenshots(instances, false);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode, instances.length]);

  // Per-instance refresh: each port runs its own 3s interval, with first-fire
  // staggered evenly across the period. This caps in-flight requests at ~ceil(1.7s/stagger),
  // staying well under Chrome's HTTP/1.1 same-origin 6-connection limit even with 7+ instances.
  const refreshOne = useCallback((port: string) => {
    if (streamingPortsRef.current.has(port)) return;
    if (fetchingRef.current.has(port)) return;
    fetchingRef.current.add(port);
    api.mhxyExecutorScreenshot(port)
      .then((d) => {
        setScreenshots((prev) => ({ ...prev, [port]: d.image_b64 }));
      })
      .catch(() => {})
      .finally(() => fetchingRef.current.delete(port));
  }, []);

  const portsKey = instances.map((i) => i.port).join(",");
  const selectedPortsKey = Array.from(selectedPorts).sort().join(",");

  useEffect(() => {
    if (viewMode !== "grid") return;
    const ports = broadcastScope === "leaders"
      ? instances.filter((i) => i.role === "leader").map((i) => i.port)
      : broadcastScope === "custom" && customSelectionActive
      ? Array.from(selectedPorts)
      : portsKey ? portsKey.split(",") : [];
    if (ports.length === 0) return;
    const period = 3_000;
    const stagger = period / ports.length;
    const timeouts: ReturnType<typeof setTimeout>[] = [];
    const intervals: ReturnType<typeof setInterval>[] = [];
    ports.forEach((port, i) => {
      const t = setTimeout(() => {
        refreshOne(port);
        const id = setInterval(() => refreshOne(port), period);
        intervals.push(id);
      }, i * stagger);
      timeouts.push(t);
    });
    return () => {
      timeouts.forEach(clearTimeout);
      intervals.forEach(clearInterval);
    };
  }, [viewMode, portsKey, selectedPortsKey, broadcastScope, customSelectionActive, refreshOne]);

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

  const allPorts = instances.map((i) => i.port);
  const leaderPorts = instances.filter((i) => i.role === "leader").map((i) => i.port);

  return (
    <div>
      {/* Modal overlay */}
      {modalPort && (
        <ScreenshotModal
          port={modalPort}
          state={modalState}
          onClose={closeModal}
          onRefreshScreenshot={refreshModalScreenshot}
        />
      )}

      {/* Page header — 视图切换和刷新固定在此行，不随模式变化 */}
      {!focusMode && (
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
      )}

      {/* 截图巡检子工具栏 — 仅 grid 模式下展示 */}
      {viewMode === "grid" && (() => {
        // Polling ports = all visible instances minus those currently streaming (skip screenshot polling).
        const visiblePorts = broadcastScope === "leaders"
          ? leaderPorts
          : broadcastScope === "custom" && customSelectionActive
          ? instances.filter((i) => selectedPorts.has(i.port)).map((i) => i.port)
          : instances.map((i) => i.port);
        const pollingPortCount = Math.max(0, visiblePorts.length - streamingPortCount);
        return (
          <div style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            flexWrap: "wrap",
            padding: "0.35rem 0.5rem",
            marginBottom: "1rem",
            border: "1px solid var(--border-hi)",
            borderRadius: "var(--r-sm)",
            background: "linear-gradient(180deg, rgba(255,255,255,0.035), rgba(255,255,255,0.012))",
            boxShadow: "inset 0 1px 0 rgba(255,255,255,0.04)",
            ...(focusMode ? { position: "sticky", top: 0, zIndex: 100 } : {}),
          }}>
            <ToolbarSection label="操作">
              <div style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 2,
                padding: 2,
                borderRadius: "var(--r-sm)",
                background: "rgba(0,0,0,0.16)",
                border: "1px solid var(--border)",
              }}>
              <ToolbarButton
                active={broadcastScope === "all"}
                tone="amber"
                onClick={() => setBroadcastScope((s) => s === "all" ? "single" : "all")}
              >
                广播
              </ToolbarButton>
              <ToolbarButton
                active={broadcastScope === "leaders"}
                tone="amber"
                onClick={() => setBroadcastScope((s) => s === "leaders" ? "single" : "leaders")}
                disabled={leaderPorts.length === 0}
                title={leaderPorts.length === 0 ? "无队长实例" : `广播至全部 ${leaderPorts.length} 个队长`}
              >
                仅队长
              </ToolbarButton>
              <ToolbarButton
                active={broadcastScope === "custom"}
                tone="blue"
                onClick={() => {
                  if (broadcastScope === "custom") {
                    // In custom mode: apply filter if selections exist, otherwise just exit
                    if (selectedPorts.size > 0) {
                      setCustomSelectionActive((v) => !v);
                    } else {
                      setBroadcastScope("single");
                    }
                  } else {
                    // Enter custom mode (selection mode, show all + checkboxes)
                    setCustomSelectionActive(false);
                    setBroadcastScope("custom");
                  }
                }}
              >
                {broadcastScope === "custom"
                  ? (customSelectionActive ? `已选 ${selectedPorts.size} 个` : `自选${selectedPorts.size > 0 ? ` (${selectedPorts.size})` : ""}`)
                  : `自选${selectedPorts.size > 0 ? ` (${selectedPorts.size})` : ""}`}
              </ToolbarButton>
              </div>
            </ToolbarSection>

            <ToolbarDivider />

            <ToolbarSection label="画质">
              <div style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 2,
                padding: 2,
                borderRadius: "var(--r-sm)",
                background: "rgba(0,0,0,0.16)",
                border: "1px solid var(--border)",
              }}>
                {(["low", "medium", "high"] as const).map((q) => (
                  <ToolbarButton
                    key={q}
                    active={nativeStreamQuality === q}
                    tone="teal"
                    onClick={() => setNativeStreamQuality(q)}
                  >
                    {NATIVE_STREAM_QUALITY_LABEL[q]}
                  </ToolbarButton>
                ))}
              </div>
            </ToolbarSection>

            <ToolbarDivider />

            <ToolbarSection label="布局">
              <div style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 2,
                padding: 2,
                borderRadius: "var(--r-sm)",
                background: "rgba(0,0,0,0.16)",
                border: "1px solid var(--border)",
              }}>
                {([1, 2, 3, 4] as const).map((c) => (
                  <ToolbarButton
                    key={c}
                    active={gridColumns === c}
                    tone="blue"
                    onClick={() => setGridColumns(c)}
                  >
                    {c}
                  </ToolbarButton>
                ))}
              </div>
            </ToolbarSection>

            <ToolbarDivider />

            <ToolbarButton
              active={focusMode}
              tone="blue"
              title={focusMode ? "退出专注模式" : "隐藏页面头部，全屏显示"}
              onClick={() => setFocusMode(!focusMode)}
            >
              专注模式
            </ToolbarButton>

            <div style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 8 }}>
              <div style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 2,
                padding: 2,
                borderRadius: "var(--r-sm)",
                background: "rgba(0,0,0,0.16)",
                border: "1px solid var(--border)",
              }}>
                <ToolbarButton
                  active={tapMode}
                  tone="green"
                  onClick={() => { setTapMode((v) => !v); if (tapMode) setBroadcastScope("single"); }}
                >
                  手势操作
                </ToolbarButton>
                <ToolbarDivider />
                <ToolbarButton
                  active={swipeEnabled}
                  tone="green"
                  onClick={() => setSwipeEnabled((v) => !v)}
                >
                  滑动
                </ToolbarButton>
              </div>

              <ToolbarDivider />
              <ToolbarSection label="链路">
                <BandwidthBadge
                  streamingPorts={streamingPortCount}
                  nativeStreamQuality={nativeStreamQuality}
                  pollingPortCount={pollingPortCount}
                  actualBitrate={actualBitrate}
                />
              </ToolbarSection>
            </div>
          </div>
      ); })()}

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
              <GroupBlock key={gid} groupId={gid} instances={insts} screenshots={screenshots} onScreenshot={openModal} isMobile={isMobile} />
            ))}

          {standalone.length > 0 && (
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: "0.5rem" }}>
                <span style={{ color: "var(--text)", fontWeight: 600, fontSize: 13 }}>独立实例</span>
              </div>
              {!isMobile && <ColumnHeaders />}
              <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
                {standalone.map((inst) => isMobile
                  ? <InstanceRowMobile key={inst.port} inst={inst} screenshot={screenshots[inst.port] ?? "idle"} onScreenshot={openModal} />
                  : <InstanceRow key={inst.port} inst={inst} screenshot={screenshots[inst.port] ?? "idle"} onScreenshot={openModal} />
                )}
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
                broadcastScope={broadcastScope}
                allPorts={allPorts}
                leaderPorts={leaderPorts}
                nativeStreamQuality={nativeStreamQuality}
                onStreamModeChange={setPortStreaming}
                selectedPorts={selectedPorts}
                togglePortSelection={togglePortSelection}
                customSelectionActive={customSelectionActive}
                gridColumns={gridColumns}
                swipeEnabled={swipeEnabled}
              />
            ))}

          {standalone.length > 0 && (() => {
            const visibleStandalone = broadcastScope === "leaders"
              ? standalone.filter((i) => i.role === "leader")
              : broadcastScope === "custom" && customSelectionActive
              ? standalone.filter((i) => selectedPorts.has(i.port))
              : standalone;
            if (visibleStandalone.length === 0) return null;
            return (
              <div style={{ marginBottom: "1.5rem" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: "0.75rem" }}>
                  <span style={{ color: "var(--text)", fontWeight: 600, fontSize: 13 }}>独立实例</span>
                </div>
                <div style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
                  gap: "0.75rem",
                }}>
                  {visibleStandalone.map((inst) => (
                    <ScreenshotCard
                      key={inst.port}
                      inst={inst}
                      screenshot={screenshots[inst.port] ?? "idle"}
                      onClick={() => openModal(inst.port)}
                      tapMode={tapMode}
                      broadcastScope={broadcastScope}
                      allPorts={allPorts}
                      leaderPorts={leaderPorts}
                      nativeStreamQuality={nativeStreamQuality}
                      onStreamModeChange={setPortStreaming}
                      isSelected={selectedPorts.has(inst.port)}
                      onToggleSelect={() => togglePortSelection(inst.port)}
                      showSelectionUI={broadcastScope === "custom" && !customSelectionActive}
                      swipeEnabled={swipeEnabled}
                    />
                  ))}
                </div>
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}

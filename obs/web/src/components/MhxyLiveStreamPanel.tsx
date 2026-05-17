import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type MhxyInstanceDetail } from "@/lib/api";
import { execWsBase } from "@/lib/gateway";
import { isWebCodecsSupported, StreamPlayer, type StreamPlayerStatus } from "@/lib/h264-stream";

type NativeStreamQuality = "low" | "medium" | "high";

const QUALITY_LABEL: Record<NativeStreamQuality, string> = {
  low: "低",
  medium: "中",
  high: "高",
};

const ROLE_LABEL: Record<string, string> = {
  leader: "队长",
  member: "队员",
  standalone: "单机",
};

function statusText(status: StreamPlayerStatus, detail: string): string {
  if (status === "connecting") return "连接中...";
  if (status === "waiting-keyframe") return "等待关键帧...";
  if (status === "playing") return "实时";
  if (status === "restarting") return "重连中...";
  if (status === "closed") return "已断开";
  if (!isWebCodecsSupported()) return "浏览器不支持 WebCodecs";
  return `解码失败${detail ? `：${detail.slice(0, 80)}` : ""}`;
}

function StatusDot({ label, ok }: { label: string; ok: boolean | null }) {
  const color = ok === true ? "var(--green)" : ok === false ? "var(--red)" : "var(--text-dim)";
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 3, color: "var(--text-dim)" }}>
      <span style={{ width: 6, height: 6, borderRadius: 999, background: color, flexShrink: 0 }} />
      {label}
    </span>
  );
}

export default function MhxyLiveStreamPanel({ width }: { width?: number }) {
  const [instances, setInstances] = useState<MhxyInstanceDetail[]>([]);
  const [selectedPort, setSelectedPort] = useState("");
  const [quality, setQuality] = useState<NativeStreamQuality>("low");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [streamStatus, setStreamStatus] = useState<StreamPlayerStatus>("closed");
  const [streamDetail, setStreamDetail] = useState("");
  const [resolution, setResolution] = useState<{ w: number; h: number } | null>(null);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const playerRef = useRef<StreamPlayer | null>(null);

  const selected = useMemo(
    () => instances.find((inst) => inst.port === selectedPort),
    [instances, selectedPort],
  );
  const streamUrl = selectedPort ? `${execWsBase()}/ws/stream/${selectedPort}?quality=${quality}` : "";

  const fetchInstances = useCallback((silent = false) => {
    if (!silent) {
      setLoading(true);
      setError("");
    }
    api.mhxyExecutorInstances()
      .then((data) => {
        const list = [...data.instances].sort((a, b) => Number(a.port) - Number(b.port));
        setInstances(list);
        setSelectedPort((prev) => list.some((inst) => inst.port === prev) ? prev : list[0]?.port || "");
        setError("");
      })
      .catch((e) => {
        setError(String(e));
      })
      .finally(() => {
        if (!silent) setLoading(false);
      });
  }, []);

  useEffect(() => {
    fetchInstances();
    const id = setInterval(() => fetchInstances(true), 12000);
    return () => clearInterval(id);
  }, [fetchInstances]);

  useEffect(() => {
    if (!streamUrl || !canvasRef.current) return;
    if (!isWebCodecsSupported()) {
      setStreamStatus("error");
      setStreamDetail("");
      return;
    }

    setStreamStatus("connecting");
    setStreamDetail("");
    setResolution(null);
    const player = new StreamPlayer({
      url: streamUrl,
      canvas: canvasRef.current,
      onStatus: (status, detail) => {
        setStreamStatus(status);
        setStreamDetail(detail ?? "");
      },
      onResolution: (w, h) => setResolution({ w, h }),
    });
    playerRef.current = player;
    return () => {
      player.close();
      playerRef.current = null;
    };
  }, [streamUrl]);

  const healthyColor = selected?.healthy === true
    ? "var(--green)"
    : selected?.healthy === false
    ? "var(--red)"
    : "var(--text-muted)";

  return (
    <aside style={{
      width: width ?? 380,
      minWidth: 280,
      maxWidth: 800,
      flexShrink: 0,
      borderLeft: "1px solid var(--border)",
      paddingLeft: "1rem",
      display: "flex",
      flexDirection: "column",
      minHeight: 0,
      gap: 10,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <h2 style={{ margin: 0, fontSize: 14, color: "var(--text)", fontWeight: 700 }}>实例实时流</h2>
        <span style={{
          width: 7,
          height: 7,
          borderRadius: 999,
          background: streamStatus === "playing" ? "var(--green)" : "var(--text-dim)",
        }} />
        <span style={{ color: streamStatus === "error" ? "var(--red)" : "var(--text-dim)", fontSize: 11 }}>
          {statusText(streamStatus, streamDetail)}
        </span>
      </div>


      <div style={{
        background: "#0d0d0d",
        aspectRatio: "16/9",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        position: "relative",
        overflow: "hidden",
        border: "1px solid var(--border)",
        borderRadius: "var(--r-sm)",
      }}>
        <canvas
          ref={canvasRef}
          style={{ width: "100%", height: "100%", objectFit: "contain", display: selectedPort ? "block" : "none" }}
        />
        {streamStatus !== "playing" && (
          <div style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: "rgba(0,0,0,0.55)",
            color: streamStatus === "error" ? "var(--red)" : "var(--text-dim)",
            fontSize: 12,
            textAlign: "center",
            padding: 12,
          }}>
            {loading ? "加载实例..." : error || (selectedPort ? statusText(streamStatus, streamDetail) : "暂无实例")}
          </div>
        )}
      </div>

      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 11 }}>
        <span style={{ color: "var(--text-dim)", fontFamily: "var(--font-mono)" }}>
          {selected ? `port:${selected.port}` : "port:-"}
        </span>
        <span style={{ color: healthyColor }}>
          {selected?.healthy === true ? "健康" : selected?.healthy === false ? "异常" : "状态未知"}
        </span>
        {resolution && (
          <span style={{ color: "var(--text-dim)", marginLeft: "auto", fontFamily: "var(--font-mono)" }}>
            {resolution.w}x{resolution.h}
          </span>
        )}
      </div>

      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "var(--text-muted)", fontSize: 12, fontWeight: 600 }}>全部实例</span>
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
            {instances.length === 0
              ? "—"
              : `${instances.filter((i) => i.healthy === true).length}/${instances.length} 健康`}
          </span>
          <div style={{
            marginLeft: "auto",
            display: "flex",
            border: "1px solid var(--border)",
            borderRadius: "var(--r-sm)",
            overflow: "hidden",
          }}>
            {(["low", "medium", "high"] as const).map((q) => {
              const active = quality === q;
              return (
                <button
                  key={q}
                  onClick={() => setQuality(q)}
                  title={`画质：${QUALITY_LABEL[q]}`}
                  style={{
                    background: active ? "var(--blue-dim)" : "transparent",
                    color: active ? "var(--blue)" : "var(--text-dim)",
                    border: "none",
                    padding: "1px 7px",
                    fontSize: 10,
                    lineHeight: 1.6,
                    cursor: "pointer",
                  }}
                >
                  {QUALITY_LABEL[q]}
                </button>
              );
            })}
          </div>
        </div>
        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", display: "flex", flexDirection: "column", gap: 4 }}>
          {loading && instances.length === 0 && (
            <p style={{ color: "var(--text-dim)", fontSize: 11, margin: 0 }}>加载实例...</p>
          )}
          {!loading && instances.length === 0 && (
            <p style={{ color: "var(--text-dim)", fontSize: 11, margin: 0 }}>暂无实例</p>
          )}
          {instances.map((inst) => {
            const isSel = inst.port === selectedPort;
            const roleLabel = ROLE_LABEL[inst.role] ?? inst.role;
            return (
              <div
                key={inst.port}
                onClick={() => setSelectedPort(inst.port)}
                title={inst.error || undefined}
                style={{
                  cursor: "pointer",
                  background: isSel ? "rgba(96,165,250,.08)" : "rgba(255,255,255,.02)",
                  border: "1px solid var(--border)",
                  borderLeftWidth: 3,
                  borderLeftColor: isSel ? "var(--blue)" : "var(--border)",
                  borderRadius: "var(--r-sm)",
                  padding: "5px 8px",
                  display: "flex",
                  flexDirection: "column",
                  gap: 3,
                }}
              >
                <div style={{ display: "flex", alignItems: "baseline", gap: 6, flexWrap: "wrap" }}>
                  <span style={{
                    fontFamily: "var(--font-mono)", fontSize: 12,
                    color: isSel ? "var(--blue)" : "var(--text)",
                  }}>
                    {inst.port}
                  </span>
                  <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    {inst.school || "未知"} · {roleLabel}
                    {inst.group_id == null ? "" : ` · G${inst.group_id}`}
                  </span>
                  {inst.latency_ms != null && (
                    <span style={{
                      marginLeft: "auto", fontSize: 10,
                      fontFamily: "var(--font-mono)", color: "var(--text-dim)",
                    }}>
                      {inst.latency_ms}ms
                    </span>
                  )}
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", fontSize: 10 }}>
                  <StatusDot label="健康" ok={inst.healthy} />
                  <StatusDot label="adb" ok={inst.adb} />
                  <StatusDot label="截图" ok={inst.screenshot} />
                  <StatusDot label="ocr" ok={inst.ocr} />
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </aside>
  );
}

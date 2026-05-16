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

function instanceLabel(inst: MhxyInstanceDetail): string {
  const role = ROLE_LABEL[inst.role] ?? inst.role;
  const group = inst.group_id == null ? "" : ` · G${inst.group_id}`;
  return `${inst.port} · ${inst.school || "未知"} · ${role}${group}`;
}

export default function MhxyLiveStreamPanel() {
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

  const fetchInstances = useCallback(() => {
    setLoading(true);
    setError("");
    api.mhxyExecutorInstances()
      .then((data) => {
        const list = [...data.instances].sort((a, b) => Number(a.port) - Number(b.port));
        setInstances(list);
        setSelectedPort((prev) => list.some((inst) => inst.port === prev) ? prev : list[0]?.port || "");
      })
      .catch((e) => {
        setError(String(e));
      })
      .finally(() => {
        setLoading(false);
      });
  }, []);

  useEffect(() => fetchInstances(), [fetchInstances]);

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
      width: "min(34vw, 460px)",
      minWidth: 340,
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

      <div style={{ display: "grid", gridTemplateColumns: "1fr auto auto", gap: 8 }}>
        <select
          value={selectedPort}
          onChange={(e) => setSelectedPort(e.target.value)}
          disabled={loading || instances.length === 0}
          style={{
            minWidth: 0,
            background: "rgba(255,255,255,.03)",
            color: "var(--text)",
            border: "1px solid var(--border)",
            borderRadius: 4,
            padding: "6px 8px",
            fontSize: 12,
          }}
        >
          {instances.map((inst) => (
            <option key={inst.port} value={inst.port}>{instanceLabel(inst)}</option>
          ))}
        </select>
        <select
          value={quality}
          onChange={(e) => setQuality(e.target.value as NativeStreamQuality)}
          style={{
            background: "rgba(255,255,255,.03)",
            color: "var(--text)",
            border: "1px solid var(--border)",
            borderRadius: 4,
            padding: "6px 8px",
            fontSize: 12,
          }}
        >
          {(["low", "medium", "high"] as const).map((q) => (
            <option key={q} value={q}>{QUALITY_LABEL[q]}</option>
          ))}
        </select>
        <button
          onClick={() => fetchInstances()}
          disabled={loading}
          title="刷新实例列表"
          style={{
            background: "rgba(255,255,255,.03)",
            color: loading ? "var(--text-dim)" : "var(--text-muted)",
            border: "1px solid var(--border)",
            borderRadius: 4,
            padding: "6px 8px",
            fontSize: 12,
            cursor: loading ? "not-allowed" : "pointer",
          }}
        >
          刷新
        </button>
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
    </aside>
  );
}

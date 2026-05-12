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
// Screenshot modal (full size)
// ---------------------------------------------------------------------------

function ScreenshotModal({
  port,
  state,
  onClose,
}: {
  port: string;
  state: ScreenshotState;
  onClose: () => void;
}) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

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
      <div style={{ color: "var(--text-muted)", fontSize: 12, marginBottom: 10 }}>
        端口 {port} — 点击任意处或按 Esc 关闭
      </div>
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: "90vw", maxHeight: "85vh", display: "flex", alignItems: "center", justifyContent: "center" }}
      >
        {state === "loading" && (
          <span style={{ color: "var(--text-dim)", fontSize: 13 }}>加载中…</span>
        )}
        {state === "error" && (
          <span style={{ color: "var(--red)", fontSize: 13 }}>截图获取失败</span>
        )}
        {state !== "loading" && state !== "error" && state !== "idle" && (
          <img
            src={`data:image/png;base64,${state}`}
            alt={`port-${port}-screenshot`}
            style={{ maxWidth: "100%", maxHeight: "85vh", borderRadius: 4, display: "block" }}
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// List view components
// ---------------------------------------------------------------------------

const LIST_COLS = "60px 80px 56px 48px 48px 48px 64px 1fr 120px";
const LIST_HEADERS = ["端口", "门派", "身份", "ADB", "截图", "OCR", "延迟", "状态", "预览"];

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
            src={`data:image/png;base64,${screenshot}`}
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

function ScreenshotCard({
  inst,
  screenshot,
  onClick,
}: {
  inst: MhxyInstanceDetail;
  screenshot: ScreenshotState;
  onClick: () => void;
}) {
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
      <div style={{
        background: "#0d0d0d",
        aspectRatio: "16/9",
        display: "flex", alignItems: "center", justifyContent: "center",
        position: "relative",
      }}>
        {screenshot === "idle" && (
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>等待加载</span>
        )}
        {screenshot === "loading" && (
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>加载中…</span>
        )}
        {screenshot === "error" && (
          <span style={{ color: "var(--red)", fontSize: 11 }}>获取失败</span>
        )}
        {screenshot !== "idle" && screenshot !== "loading" && screenshot !== "error" && (
          <img
            src={`data:image/png;base64,${screenshot}`}
            alt={`port-${inst.port}`}
            style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
          />
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
}: {
  groupId: number;
  instances: MhxyInstanceDetail[];
  screenshots: Record<string, ScreenshotState>;
  onCardClick: (port: string) => void;
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
    const id = setInterval(autoRefresh, 15_000);
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

  return (
    <div>
      {/* Modal overlay */}
      {modalPort && (
        <ScreenshotModal port={modalPort} state={modalState} onClose={closeModal} />
      )}

      {/* Page header */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: "1.5rem", flexWrap: "wrap" }}>
        <Link href="/" style={{ color: "var(--text-muted)", fontSize: 13 }}>← 返回总览</Link>
        <span style={{ color: "var(--border-hi)" }}>|</span>
        <h1 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)", margin: 0 }}>
          Windows Executor 实例详情
        </h1>

        {/* View mode toggle + actions */}
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
          <button
            onClick={refreshScreenshots}
            style={{
              padding: "4px 10px",
              borderRadius: "var(--r-sm)",
              border: "1px solid var(--border-hi)",
              background: "transparent",
              color: "var(--blue)",
              fontSize: 11, fontWeight: 500, cursor: "pointer",
            }}
          >
            刷新截图
          </button>
          <button
            onClick={() => { setLoading(true); load().finally(() => setLoading(false)); }}
            style={{
              padding: "4px 10px",
              borderRadius: "var(--r-sm)",
              border: "1px solid var(--border-hi)",
              background: "transparent",
              color: "var(--text-muted)",
              fontSize: 11, fontWeight: 500, cursor: "pointer",
            }}
          >
            刷新状态
          </button>
        </div>
      </div>

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
            {viewMode === "grid" && " · 点击卡片可放大"}
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

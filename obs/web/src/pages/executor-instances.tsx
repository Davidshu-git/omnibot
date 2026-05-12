import { useCallback, useEffect, useState } from "react";
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

function CheckIcon({ ok }: { ok: boolean | null }) {
  if (ok === null || ok === undefined) return <span style={{ color: "var(--text-dim)" }}>—</span>;
  return ok
    ? <span style={{ color: "var(--green)", fontWeight: 700 }}>✓</span>
    : <span style={{ color: "var(--red)", fontWeight: 700 }}>✗</span>;
}

function InstanceRow({ inst }: { inst: MhxyInstanceDetail }) {
  const statusColor = inst.healthy === true
    ? "var(--green)"
    : inst.healthy === false
    ? "var(--red)"
    : "var(--text-muted)";

  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "60px 80px 56px 48px 48px 48px 64px 1fr",
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
    </div>
  );
}

function GroupBlock({ groupId, instances }: { groupId: number; instances: MhxyInstanceDetail[] }) {
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

      {/* column headers */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "60px 80px 56px 48px 48px 48px 64px 1fr",
        gap: "0.5rem",
        padding: "0 0.75rem",
        marginBottom: "0.35rem",
      }}>
        {["端口", "门派", "身份", "ADB", "截图", "OCR", "延迟", "状态"].map((h) => (
          <span key={h} className="stat-label" style={{ fontSize: 11 }}>{h}</span>
        ))}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
        {leader && <InstanceRow inst={leader} />}
        {members.map((m) => <InstanceRow key={m.port} inst={m} />)}
      </div>
    </div>
  );
}

export default function ExecutorInstancesPage() {
  const [data, setData] = useState<MhxyExecutorInstances | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

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

  // Group by group_id; standalone instances have group_id = null
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
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: "1.5rem" }}>
        <Link href="/" style={{ color: "var(--text-muted)", fontSize: 13 }}>← 返回总览</Link>
        <span style={{ color: "var(--border-hi)" }}>|</span>
        <h1 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)", margin: 0 }}>
          Windows Executor 实例详情
        </h1>
        <button
          onClick={() => { setLoading(true); load().finally(() => setLoading(false)); }}
          style={{
            marginLeft: "auto",
            padding: "4px 10px",
            borderRadius: "var(--r-sm)",
            border: "1px solid var(--border-hi)",
            background: "transparent",
            color: "var(--blue)",
            fontSize: 11,
            fontWeight: 500,
          }}
        >
          刷新
        </button>
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
      ) : (
        <div className="card">
          {Array.from(groups.entries())
            .sort(([a], [b]) => a - b)
            .map(([gid, insts]) => (
              <GroupBlock key={gid} groupId={gid} instances={insts} />
            ))}

          {standalone.length > 0 && (
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: "0.5rem" }}>
                <span style={{ color: "var(--text)", fontWeight: 600, fontSize: 13 }}>独立实例</span>
              </div>
              <div style={{
                display: "grid",
                gridTemplateColumns: "60px 80px 56px 48px 48px 48px 64px 1fr",
                gap: "0.5rem",
                padding: "0 0.75rem",
                marginBottom: "0.35rem",
              }}>
                {["端口", "门派", "身份", "ADB", "截图", "OCR", "延迟", "状态"].map((h) => (
                  <span key={h} className="stat-label" style={{ fontSize: 11 }}>{h}</span>
                ))}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
                {standalone.map((inst) => <InstanceRow key={inst.port} inst={inst} />)}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

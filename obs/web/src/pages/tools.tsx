import { useEffect, useState } from "react";
import { api, type ToolStat } from "@/lib/api";
import type { Project } from "@/types/events";

export default function ToolsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<string>("");
  const [tools, setTools] = useState<ToolStat[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.projects().then((ps) => {
      setProjects(ps);
      if (ps.length > 0) setSelectedProject(ps[0].id);
    }).catch((e) => setErr(String(e)));
  }, []);

  useEffect(() => {
    if (!selectedProject) return;
    api.tools(selectedProject).then(setTools).catch((e) => setErr(String(e)));
  }, [selectedProject]);

  const maxCalls = tools.length > 0 ? tools[0].calls : 1;

  const RANK_COLORS = ["var(--amber)", "var(--orange)", "var(--blue)"];

  return (
    <div>
      <div style={{ marginBottom: "1.5rem" }}>
        <h1 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)", marginBottom: 4 }}>工具调用分析</h1>
        <p style={{ color: "var(--text-muted)", fontSize: 12 }}>各 Agent 工具使用频率排行</p>
      </div>

      {err && <p style={{ color: "var(--red)", marginBottom: "1rem" }}>{err}</p>}

      <div style={{ display: "flex", gap: 6, marginBottom: "1.5rem" }}>
        {projects.map((p) => (
          <button
            key={p.id}
            onClick={() => setSelectedProject(p.id)}
            className={`tag-btn${selectedProject === p.id ? " active" : ""}`}
          >
            {p.display_name}
          </button>
        ))}
      </div>

      {tools.length > 0 ? (
        <div className="card" style={{ maxWidth: 520 }}>
          <div style={{ color: "var(--text-muted)", fontSize: 12, marginBottom: "1rem" }}>
            共 <strong style={{ color: "var(--text)" }}>{tools.length}</strong> 种工具
          </div>
          {tools.map((t, i) => {
            const barPct = Math.round((t.calls / maxCalls) * 100);
            const color = RANK_COLORS[i] ?? "var(--text-dim)";
            return (
              <div key={t.tool_name} style={{ marginBottom: "0.75rem" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{
                      color: i < 3 ? color : "var(--text-dim)",
                      fontSize: 10, fontWeight: 700, width: 16, textAlign: "right",
                      fontFamily: "var(--font-mono)",
                    }}>
                      {i + 1}
                    </span>
                    <span style={{
                      color: i < 3 ? "var(--text)" : "var(--text-muted)",
                      fontSize: 12, fontFamily: "var(--font-mono)",
                    }}>
                      {t.tool_name}
                    </span>
                  </div>
                  <span style={{ color, fontWeight: 700, fontSize: 12, fontFamily: "var(--font-mono)" }}>
                    {t.calls}
                  </span>
                </div>
                <div style={{ background: "var(--border)", borderRadius: 3, height: 5, overflow: "hidden", marginLeft: 24 }}>
                  <div style={{
                    width: `${barPct}%`, height: "100%",
                    background: i < 3 ? color : "var(--border-hi)",
                    transition: "width 0.4s",
                  }} />
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        !err && <p style={{ color: "var(--text-dim)" }}>暂无工具调用数据，请先同步日志。</p>
      )}
    </div>
  );
}

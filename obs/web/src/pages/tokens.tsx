import { useEffect, useState } from "react";
import { useRouter } from "next/router";
import Link from "next/link";
import { api, type ProjectOverview, type TokenOverview, type TokenDailyStat, type TokenByModel } from "@/lib/api";
import type { Project } from "@/types/events";
import { fmt, fmtCost } from "@/lib/format";
import { useIsMobile } from "@/lib/useIsMobile";

export default function TokensPage() {
  const ALL = "__all__";
  const router = useRouter();
  const isMobile = useIsMobile();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectOverviews, setProjectOverviews] = useState<ProjectOverview[]>([]);
  const [selectedProject, setSelectedProject] = useState<string>(ALL);

  useEffect(() => {
    if (!router.isReady) return;
    const pid = router.query.project_id;
    if (typeof pid === "string" && pid) setSelectedProject(pid);
  }, [router.isReady, router.query.project_id]);
  const [overview, setOverview] = useState<TokenOverview | null>(null);
  const [daily, setDaily] = useState<TokenDailyStat[]>([]);
  const [byModel, setByModel] = useState<TokenByModel[]>([]);
  const [projectStats, setProjectStats] = useState<Record<string, TokenOverview>>({});
  const [days, setDays] = useState(30);
  const [distributionMode, setDistributionMode] = useState<"project" | "model">("project");
  const [err, setErr] = useState("");

  useEffect(() => {
    api.projects().then(setProjects).catch((e) => setErr(String(e)));
    api.overview().then(setProjectOverviews).catch((e) => setErr(String(e)));
  }, []);

  const projectIdParam = selectedProject === ALL ? undefined : selectedProject;

  useEffect(() => {
    api.tokensOverview(projectIdParam).then(setOverview).catch((e) => setErr(String(e)));
    api.tokensDaily(projectIdParam, days).then(setDaily).catch((e) => setErr(String(e)));
    api.tokensByModel(projectIdParam).then(setByModel).catch((e) => setErr(String(e)));
  }, [selectedProject, days]);

  useEffect(() => {
    if (selectedProject !== ALL || projects.length === 0) return;
    Promise.all(projects.map((p) => api.tokensOverview(p.id).then((ov) => [p.id, ov] as const)))
      .then((entries) => setProjectStats(Object.fromEntries(entries)))
      .catch((e) => setErr(String(e)));
  }, [selectedProject, projects]);

  const total = overview ? overview.input_tokens + overview.output_tokens : 0;
  const inputPct = overview && total > 0 ? Math.round((overview.input_tokens / total) * 100) : 0;
  const outputPct = total > 0 ? 100 - inputPct : 0;
  const totalCost = byModel.reduce((s, m) => s + (m.cost ?? 0), 0);
  const hasCost = byModel.some((m) => m.cost !== null);
  const selectedProjectMeta = projects.find((p) => p.id === selectedProject);
  const projectCostMap = Object.fromEntries(projectOverviews.map((p) => [p.project_id, p.total_cost] as const));
  const projectRows = overview
    ? selectedProject === ALL
      ? projects
          .map((p) => ({ id: p.id, name: p.display_name, stat: projectStats[p.id] }))
          .filter((row): row is { id: string; name: string; stat: TokenOverview } => Boolean(row.stat))
          .map((row) => {
            const rowTotal = row.stat.input_tokens + row.stat.output_tokens;
            return {
              key: row.id,
              name: row.name,
              calls: row.stat.calls,
              inputTokens: row.stat.input_tokens,
              outputTokens: row.stat.output_tokens,
              cacheReadTokens: row.stat.cache_read_tokens,
              totalTokens: rowTotal,
              pct: total > 0 ? Math.round((rowTotal / total) * 100) : 0,
              cost: projectCostMap[row.id] ?? null,
            };
          })
      : [{
          key: selectedProject,
          name: selectedProjectMeta?.display_name ?? selectedProject,
          calls: overview.calls,
          inputTokens: overview.input_tokens,
          outputTokens: overview.output_tokens,
          cacheReadTokens: overview.cache_read_tokens,
          totalTokens: total,
          pct: total > 0 ? 100 : 0,
          cost: projectCostMap[selectedProject] ?? null,
        }]
    : [];
  const modelTotal = byModel.reduce((s, m) => s + m.input_tokens + m.output_tokens, 0);
  const modelRows = byModel.map((m) => {
    const rowTotal = m.input_tokens + m.output_tokens;
    return {
      key: m.model,
      name: m.model,
      calls: m.calls,
      inputTokens: m.input_tokens,
      outputTokens: m.output_tokens,
      cacheReadTokens: m.cache_read_tokens,
      totalTokens: rowTotal,
      pct: modelTotal > 0 ? Math.round((rowTotal / modelTotal) * 100) : 0,
      cost: m.cost,
    };
  });
  const distributionRows = distributionMode === "project" ? projectRows : modelRows;
  const distributionLabel = distributionMode === "project" ? "项目" : "模型";

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "1.5rem" }}>
        <Link href="/" style={{ color: "var(--text-muted)", fontSize: 13 }}>← 返回总览</Link>
        <span style={{ color: "var(--border-hi)" }}>|</span>
        <h1 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)", margin: 0 }}>Token 统计</h1>
      </div>

      {err && <p style={{ color: "var(--red)", marginBottom: "1rem" }}>{err}</p>}

      {/* project tabs */}
      <div style={{ display: "flex", gap: 6, marginBottom: "1.5rem", flexWrap: "wrap", rowGap: 6 }}>
        <button
          onClick={() => setSelectedProject(ALL)}
          className={`tag-btn${selectedProject === ALL ? " active" : ""}`}
        >
          全部
        </button>
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

      {overview && (
        <div style={{
          display: "grid",
          gridTemplateColumns: isMobile ? "1fr" : "repeat(2, minmax(0, 1fr))",
          gap: "1rem",
          width: "100%",
        }}>
          {/* usage summary card */}
          <div className="card" style={{
            padding: "0.85rem 1.1rem",
            minWidth: 0,
          }}>
            <SummaryCardTitle>LLM 调用与费用</SummaryCardTitle>
            <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: "1rem" }}>
              <div>
                <div style={{ color: "var(--text-muted)", fontSize: 12, marginBottom: 4 }}>调用次数</div>
                <div style={{ fontSize: 24, fontWeight: 800, color: "var(--amber)", fontVariantNumeric: "tabular-nums", lineHeight: 1.15 }}>
                  {overview.calls.toLocaleString()}
                </div>
              </div>
              <div>
                <div style={{ color: "var(--text-muted)", fontSize: 12, marginBottom: 4 }}>估算费用</div>
                <div style={{ fontSize: 24, fontWeight: 800, color: hasCost ? "var(--amber)" : "var(--text-dim)", fontVariantNumeric: "tabular-nums", lineHeight: 1.15 }}>
                  {hasCost ? fmtCost(totalCost) : "包月"}
                </div>
              </div>
            </div>
          </div>

          {/* token breakdown card */}
          <div className="card" style={{
            padding: "0.85rem 1.1rem",
            display: "flex",
            flexDirection: "column",
            gap: "0.4rem",
            minWidth: 0,
          }}>
            <SummaryCardTitle>Token 分布</SummaryCardTitle>
            <div style={{
              display: "flex",
              alignItems: "baseline",
              gap: "0.85rem",
              flexWrap: "wrap",
              fontVariantNumeric: "tabular-nums",
              fontFeatureSettings: "\"tnum\"",
            }}>
              <span style={{ color: "var(--text)", fontWeight: 700, fontSize: 16 }}>
                {fmt(total)}
                <span style={{ color: "var(--text-dim)", fontWeight: 400, fontSize: 11, marginLeft: 3 }}>tok</span>
              </span>
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                <span style={{ color: "var(--blue)" }}>↑</span> {fmt(overview.input_tokens)}
              </span>
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                <span style={{ color: "var(--green)" }}>↓</span> {fmt(overview.output_tokens)}
              </span>
              <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{inputPct}% 输入 · {outputPct}% 输出</span>
            </div>
            <div style={{ display: "flex", height: 3, borderRadius: 2, overflow: "hidden", background: "var(--border)" }}>
              <div style={{ width: `${inputPct}%`, background: "var(--blue)", transition: "width 0.5s var(--ease)" }} />
              <div style={{ flex: 1, background: "var(--green)" }} />
            </div>
          </div>

        </div>
      )}

      {!overview && !err && (
        <p style={{ color: "var(--text-dim)" }}>暂无 Token 数据，请先同步日志。</p>
      )}

      {/* daily chart */}
      <div style={{ marginTop: "2rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "1rem" }}>
          <span style={{ color: "var(--text-muted)", fontSize: 13, fontWeight: 600 }}>每日用量</span>
          {([7, 14, 30] as const).map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`tag-btn${days === d ? " active" : ""}`}
              style={{ fontSize: 11 }}
            >
              {d}天
            </button>
          ))}
        </div>
        {daily.length > 0 ? <DailyChart data={daily} /> : (
          <p style={{ color: "var(--text-dim)", fontSize: 12 }}>暂无每日数据</p>
        )}
      </div>

      {/* distribution table */}
      {overview && (projectRows.length > 0 || modelRows.length > 0) && (
        <div style={{ marginTop: "1.5rem" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.75rem" }}>
            <span style={{ color: "var(--text-muted)", fontSize: 13, fontWeight: 600 }}>分布明细</span>
            {(["project", "model"] as const).map((mode) => (
              <button
                key={mode}
                onClick={() => setDistributionMode(mode)}
                className={`tag-btn${distributionMode === mode ? " active" : ""}`}
                style={{ fontSize: 11, minWidth: 42 }}
              >
                {mode === "project" ? "项目" : "模型"}
              </button>
            ))}
          </div>
          <div className="card" style={{ padding: 0, overflow: "hidden" }}>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 760, tableLayout: "fixed" }}>
                <colgroup>
                  <col style={{ width: 180 }} />
                  <col style={{ width: 86 }} />
                  <col style={{ width: 92 }} />
                  <col style={{ width: 92 }} />
                  <col style={{ width: 104 }} />
                  <col style={{ width: 92 }} />
                  <col style={{ width: 116 }} />
                  <col style={{ width: 82 }} />
                </colgroup>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    {[distributionLabel, "调用次数", "输入", "输出", "缓存命中", "合计", "占比", "费用"].map((h) => (
                      <th key={h} style={{ padding: "8px 12px", textAlign: h === distributionLabel ? "left" : "right", color: "var(--text-muted)", fontWeight: 600, whiteSpace: "nowrap" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {distributionRows.map((row, i) => (
                    <tr key={row.key} style={{ borderBottom: i < distributionRows.length - 1 ? "1px solid var(--border)" : undefined }}>
                      <td style={{ padding: "8px 12px", color: "var(--text)", fontWeight: distributionMode === "project" ? 500 : 400, fontFamily: distributionMode === "model" ? "var(--font-mono)" : undefined, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={row.name}>{row.name}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--amber)", fontVariantNumeric: "tabular-nums" }}>{row.calls.toLocaleString()}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--blue)", fontFamily: "var(--font-mono)" }}>{fmt(row.inputTokens)}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--green)", fontFamily: "var(--font-mono)" }}>{fmt(row.outputTokens)}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right" }}>
                        {row.cacheReadTokens > 0 ? (
                          <span style={{ fontFamily: "var(--font-mono)" }}>
                            <span style={{ color: "var(--text)" }}>{fmt(row.cacheReadTokens)}</span>
                            {row.inputTokens > 0 && (
                              <span style={{ color: "var(--text-dim)", fontSize: 10, marginLeft: 4 }}>
                                {Math.round(row.cacheReadTokens / row.inputTokens * 100)}%
                              </span>
                            )}
                          </span>
                        ) : (
                          <span style={{ color: "var(--text-dim)" }}>—</span>
                        )}
                      </td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text)", fontWeight: 600, fontFamily: "var(--font-mono)" }}>{fmt(row.totalTokens)}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", minWidth: 90 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "flex-end" }}>
                          <div style={{ width: 60, height: 4, background: "var(--border)", borderRadius: 2, overflow: "hidden" }}>
                            <div style={{ width: `${row.pct}%`, height: "100%", background: "var(--blue)", borderRadius: 2 }} />
                          </div>
                          <span style={{ color: "var(--text-dim)", fontSize: 11, minWidth: 28, textAlign: "right" }}>{row.pct}%</span>
                        </div>
                      </td>
                      <td style={{ padding: "8px 12px", textAlign: "right" }}>
                        {row.cost !== null
                          ? <span style={{ color: "var(--amber)", fontFamily: "var(--font-mono)", fontWeight: 600 }}>{fmtCost(row.cost)}</span>
                          : <span style={{ color: "var(--text-dim)", fontSize: 11 }}>包月</span>
                        }
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function SummaryCardTitle({ children }: { children: React.ReactNode }) {
  return (
    <span style={{
      display: "block",
      color: "var(--text-muted)",
      fontSize: 11,
      fontWeight: 600,
      letterSpacing: "0.04em",
      textTransform: "uppercase",
      marginBottom: "0.475rem",
    }}>
      {children}
    </span>
  );
}

function DailyChart({ data }: { data: TokenDailyStat[] }) {
  const sorted = [...data].sort((a, b) => a.date.localeCompare(b.date));
  const isMobile = useIsMobile();
  const W = isMobile ? 360 : 640;
  const H = isMobile ? 200 : 180;
  const PAD_L = isMobile ? 40 : 52, PAD_B = 28, PAD_T = 12, PAD_R = 12;
  const chartW = W - PAD_L - PAD_R;
  const chartH = H - PAD_T - PAD_B;
  const barW = Math.max(4, Math.floor(chartW / sorted.length) - 3);
  const step = chartW / sorted.length;

  const [mode, setMode] = useState<"token" | "cost">("token");
  const [hovered, setHovered] = useState<number | null>(null);

  const hasCost = sorted.some((d) => d.cost != null);
  const MODEL_PALETTE = ["var(--cat-1)", "var(--cat-2)", "var(--cat-3)"];
  const allModels = Array.from(new Set(sorted.flatMap((d) => [
    ...(d.model_tokens ?? []).map((mt) => mt.model),
    ...(d.model_costs ?? []).map((mc) => mc.model),
  ])));
  const modelColor = (m: string) => MODEL_PALETTE[allModels.indexOf(m) % MODEL_PALETTE.length];

  const maxVal = mode === "cost"
    ? Math.max(...sorted.map((d) => d.cost ?? 0), 0.001)
    : Math.max(...sorted.map((d) => (d.model_tokens ?? []).reduce((s, mt) => s + mt.total_tokens, 0)), 1);
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) =>
    mode === "cost" ? maxVal * f : Math.round(maxVal * f)
  );

  return (
    <div className="card" style={{ padding: "1rem", overflowX: "auto" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "1rem", marginBottom: "0.75rem" }}>
        {allModels.map((m) => (
          <span key={m} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "var(--text-muted)" }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: modelColor(m), display: "inline-block" }} />
            {m}
          </span>
        ))}
        {hasCost && (
          <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
            {(["token", "cost"] as const).map((m) => (
              <button key={m} onClick={() => setMode(m)}
                className={`tag-btn${mode === m ? " active" : ""}`}
                style={{ fontSize: 10, padding: "2px 8px" }}>
                {m === "token" ? "Token" : "费用"}
              </button>
            ))}
          </div>
        )}
      </div>

      <svg width={W} height={H} style={{ display: "block", overflow: "visible" }}>
        {yTicks.map((v) => {
          const y = PAD_T + chartH - (v / maxVal) * chartH;
          return (
            <g key={v}>
              <line x1={PAD_L} x2={PAD_L + chartW} y1={y} y2={y} stroke="var(--border)" strokeWidth={0.5} />
              <text x={PAD_L - 6} y={y + 4} textAnchor="end" fontSize={9} fill="var(--text-dim)">
                {mode === "cost" ? (v === 0 ? "0" : fmtCost(v)) : fmt(v as number)}
              </text>
            </g>
          );
        })}

        {sorted.map((d, i) => {
          const x = PAD_L + i * step + (step - barW) / 2;
          const isHov = hovered === i;
          return (
            <g key={d.date} onMouseEnter={() => setHovered(i)} onMouseLeave={() => setHovered(null)} style={{ cursor: "default" }}>
              {mode === "token" ? (() => {
                const mts = allModels
                  .map((m) => ({ model: m, total_tokens: (d.model_tokens ?? []).find((mt) => mt.model === m)?.total_tokens ?? 0 }))
                  .filter((mt) => mt.total_tokens > 0);
                let stackY = PAD_T + chartH;
                return (
                  <>
                    {mts.map((mt) => {
                      const h = (mt.total_tokens / maxVal) * chartH;
                      stackY -= h;
                      return (
                        <rect key={mt.model} x={x} y={stackY} width={barW} height={h}
                          fill={modelColor(mt.model)} opacity={isHov ? 1 : 0.75} rx={1} />
                      );
                    })}
                    {isHov && (() => {
                      const tipH = 14 + mts.length * 12 + 12;
                      const tipX = Math.min(x + barW / 2 - 48, W - PAD_R - 96);
                      return (
                        <g>
                          <rect x={tipX} y={PAD_T} width={96} height={tipH} fill="var(--surface)" stroke="var(--border)" strokeWidth={1} rx={4} />
                          <text x={tipX + 48} y={PAD_T + 11} textAnchor="middle" fontSize={9} fill="var(--text)" fontWeight={600}>{d.date}</text>
                          {mts.map((mt, mi) => (
                            <text key={mt.model} x={tipX + 48} y={PAD_T + 11 + (mi + 1) * 12} textAnchor="middle" fontSize={9} fill={modelColor(mt.model)}>
                              {mt.model.split("-").slice(-2).join("-")} {fmt(mt.total_tokens)}
                            </text>
                          ))}
                          <text x={tipX + 48} y={PAD_T + 11 + (mts.length + 1) * 12} textAnchor="middle" fontSize={9} fill="var(--text-dim)">{d.calls} 次调用</text>
                        </g>
                      );
                    })()}
                  </>
                );
              })() : (() => {
                const mcMap = Object.fromEntries((d.model_costs ?? []).map((mc) => [mc.model, mc.cost]));
                const mcs = allModels.filter((m) => mcMap[m] != null).map((m) => ({ model: m, cost: mcMap[m] }));
                let stackY = PAD_T + chartH;
                return (
                  <>
                    {mcs.map((mc) => {
                      const h = (mc.cost / maxVal) * chartH;
                      stackY -= h;
                      return (
                        <rect key={mc.model} x={x} y={stackY} width={barW} height={h}
                          fill={modelColor(mc.model)} opacity={isHov ? 1 : 0.75} rx={1} />
                      );
                    })}
                    {isHov && (() => {
                      const tipH = 14 + mcs.length * 12 + 12;
                      const tipX = Math.min(x + barW / 2 - 48, W - PAD_R - 96);
                      return (
                        <g>
                          <rect x={tipX} y={PAD_T} width={96} height={tipH} fill="var(--surface)" stroke="var(--border)" strokeWidth={1} rx={4} />
                          <text x={tipX + 48} y={PAD_T + 11} textAnchor="middle" fontSize={9} fill="var(--text)" fontWeight={600}>{d.date}</text>
                          {mcs.map((mc, mi) => (
                            <text key={mc.model} x={tipX + 48} y={PAD_T + 11 + (mi + 1) * 12} textAnchor="middle" fontSize={9} fill={modelColor(mc.model)}>
                              {mc.model.split("-").slice(-2).join("-")} {fmtCost(mc.cost)}
                            </text>
                          ))}
                          <text x={tipX + 48} y={PAD_T + 11 + (mcs.length + 1) * 12} textAnchor="middle" fontSize={9} fill="var(--text-dim)">{d.calls} 次调用</text>
                        </g>
                      );
                    })()}
                  </>
                );
              })()}
              <text x={x + barW / 2} y={H - 6} textAnchor="middle" fontSize={9} fill="var(--text-dim)">{d.date.slice(5)}</text>
            </g>
          );
        })}

        <line x1={PAD_L} x2={PAD_L} y1={PAD_T} y2={PAD_T + chartH} stroke="var(--border)" strokeWidth={1} />
      </svg>
    </div>
  );
}

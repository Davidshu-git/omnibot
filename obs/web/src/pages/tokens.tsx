import { useEffect, useState } from "react";
import { api, type TokenOverview, type TokenDailyStat, type TokenByModel } from "@/lib/api";
import type { Project } from "@/types/events";
import { fmt, fmtCost } from "@/lib/format";

function Bar({ value, max, color }: { value: number; max: number; color: string }) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0;
  return (
    <div style={{ background: "var(--border)", borderRadius: 3, height: 6, overflow: "hidden" }}>
      <div style={{ width: `${pct}%`, background: color, height: "100%", transition: "width 0.4s" }} />
    </div>
  );
}

export default function TokensPage() {
  const ALL = "__all__";
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<string>(ALL);
  const [overview, setOverview] = useState<TokenOverview | null>(null);
  const [daily, setDaily] = useState<TokenDailyStat[]>([]);
  const [byModel, setByModel] = useState<TokenByModel[]>([]);
  const [projectStats, setProjectStats] = useState<Record<string, TokenOverview>>({});
  const [days, setDays] = useState(14);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.projects().then(setProjects).catch((e) => setErr(String(e)));
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
  const totalCost = byModel.reduce((s, m) => s + (m.cost ?? 0), 0);
  const hasCost = byModel.some((m) => m.cost !== null);

  return (
    <div>
      <div style={{ marginBottom: "1.5rem" }}>
        <h1 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)", marginBottom: 4 }}>Token 统计</h1>
        <p style={{ color: "var(--text-muted)", fontSize: 12 }}>各项目 LLM 调用消耗分析</p>
      </div>

      {err && <p style={{ color: "var(--red)", marginBottom: "1rem" }}>{err}</p>}

      {/* project tabs */}
      <div style={{ display: "flex", gap: 6, marginBottom: "1.5rem" }}>
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
        <div style={{ display: "flex", flexWrap: "wrap", gap: "1rem" }}>
          {/* calls card */}
          <div className="card" style={{ minWidth: 200 }}>
            <div className="stat-label">LLM 调用次数</div>
            <div style={{ fontSize: 36, fontWeight: 800, color: "var(--amber)", fontVariantNumeric: "tabular-nums", marginTop: 8 }}>
              {overview.calls.toLocaleString()}
            </div>
            <div style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 4 }}>次 model_call 事件</div>
          </div>

          {/* token breakdown card */}
          <div className="card" style={{ minWidth: 340, flex: 1, maxWidth: 480 }}>
            <div className="stat-label" style={{ marginBottom: "1rem" }}>Token 分布</div>

            <TokenRow label="输入" value={fmt(overview.input_tokens)} raw={overview.input_tokens} max={total} color="var(--blue)" />
            <TokenRow label="输出" value={fmt(overview.output_tokens)} raw={overview.output_tokens} max={total} color="var(--green)" />

            <div style={{ marginTop: "1rem", paddingTop: "0.75rem", borderTop: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ color: "var(--text-muted)", fontSize: 12 }}>合计（输入 + 输出）</span>
              <span style={{ color: "var(--text)", fontWeight: 700, fontSize: 15, fontVariantNumeric: "tabular-nums" }}>
                {fmt(total)}
              </span>
            </div>
          </div>

          {/* cost summary card */}
          {hasCost && (
            <div className="card" style={{ minWidth: 200 }}>
              <div className="stat-label">按量计费 · 估算费用</div>
              <div style={{ fontSize: 32, fontWeight: 800, color: "var(--amber)", fontVariantNumeric: "tabular-nums", marginTop: 8 }}>
                {fmtCost(totalCost)}
              </div>
              <div style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 4 }}>仅含 DeepSeek / Qwen-VL</div>
            </div>
          )}
        </div>
      )}

      {!overview && !err && (
        <p style={{ color: "var(--text-dim)" }}>暂无 Token 数据，请先同步日志。</p>
      )}

      {/* all-projects breakdown table */}
      {selectedProject === ALL && byModel.length > 0 && overview && (
        <div style={{ marginTop: "1.5rem" }}>
          <div style={{ color: "var(--text-muted)", fontSize: 13, fontWeight: 600, marginBottom: "0.75rem" }}>各项目占比</div>
          <div className="card" style={{ padding: 0, overflow: "hidden" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  {["项目", "调用次数", "输入", "输出", "合计", "占比"].map((h) => (
                    <th key={h} style={{ padding: "8px 12px", textAlign: h === "项目" ? "left" : "right", color: "var(--text-muted)", fontWeight: 600 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {projects.map((p, i) => {
                  const proj = projectStats[p.id];
                  if (!proj) return null;
                  const grandTotal = overview.input_tokens + overview.output_tokens;
                  const projTotal = proj.input_tokens + proj.output_tokens;
                  const pct = grandTotal > 0 ? Math.round((projTotal / grandTotal) * 100) : 0;
                  return (
                    <tr key={p.id} style={{ borderBottom: i < projects.length - 1 ? "1px solid var(--border)" : undefined }}>
                      <td style={{ padding: "8px 12px", color: "var(--text)", fontWeight: 500 }}>{p.display_name}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--amber)", fontVariantNumeric: "tabular-nums" }}>{proj.calls.toLocaleString()}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--blue)", fontFamily: "var(--font-mono)" }}>{fmt(proj.input_tokens)}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--green)", fontFamily: "var(--font-mono)" }}>{fmt(proj.output_tokens)}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text)", fontWeight: 600, fontFamily: "var(--font-mono)" }}>{fmt(projTotal)}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", minWidth: 90 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "flex-end" }}>
                          <div style={{ width: 60, height: 4, background: "var(--border)", borderRadius: 2, overflow: "hidden" }}>
                            <div style={{ width: `${pct}%`, height: "100%", background: "var(--blue)", borderRadius: 2 }} />
                          </div>
                          <span style={{ color: "var(--text-dim)", fontSize: 11, minWidth: 28, textAlign: "right" }}>{pct}%</span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* by-model table */}
      {byModel.length > 0 && (
        <div style={{ marginTop: "2rem" }}>
          <div style={{ color: "var(--text-muted)", fontSize: 13, fontWeight: 600, marginBottom: "0.75rem" }}>按模型分布</div>
          <div className="card" style={{ padding: 0, overflow: "hidden" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  {["模型", "调用次数", "输入", "缓存命中", "输出", "合计", "费用"].map((h) => (
                    <th key={h} style={{ padding: "8px 12px", textAlign: h === "模型" ? "left" : "right", color: "var(--text-muted)", fontWeight: 600 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {byModel.map((m, i) => {
                  const total = m.input_tokens + m.output_tokens;
                  const grandTotal = byModel.reduce((s, x) => s + x.input_tokens + x.output_tokens, 0);
                  const pct = grandTotal > 0 ? Math.round((total / grandTotal) * 100) : 0;
                  return (
                    <tr key={m.model} style={{ borderBottom: i < byModel.length - 1 ? "1px solid var(--border)" : undefined }}>
                      <td style={{ padding: "8px 12px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <span style={{ color: "var(--text)", fontFamily: "var(--font-mono)" }}>{m.model}</span>
                          <span style={{ fontSize: 10, color: "var(--text-dim)", background: "var(--surface-alt)", padding: "1px 5px", borderRadius: 3 }}>{pct}%</span>
                        </div>
                        <div style={{ marginTop: 4, height: 3, background: "var(--border)", borderRadius: 2, overflow: "hidden" }}>
                          <div style={{ width: `${pct}%`, height: "100%", background: "var(--blue)", borderRadius: 2 }} />
                        </div>
                      </td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--amber)", fontVariantNumeric: "tabular-nums" }}>{m.calls.toLocaleString()}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--blue)", fontFamily: "var(--font-mono)" }}>{fmt(m.input_tokens)}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right" }}>
                        {m.cache_read_tokens > 0 ? (
                          <span style={{ fontFamily: "var(--font-mono)" }}>
                            <span style={{ color: "var(--text)" }}>{fmt(m.cache_read_tokens)}</span>
                            <span style={{ color: "var(--text-dim)", fontSize: 10, marginLeft: 4 }}>
                              {Math.round(m.cache_read_tokens / m.input_tokens * 100)}%
                            </span>
                          </span>
                        ) : (
                          <span style={{ color: "var(--text-dim)" }}>—</span>
                        )}
                      </td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--green)", fontFamily: "var(--font-mono)" }}>{fmt(m.output_tokens)}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right", color: "var(--text)", fontWeight: 600, fontFamily: "var(--font-mono)" }}>{fmt(total)}</td>
                      <td style={{ padding: "8px 12px", textAlign: "right" }}>
                        {m.cost !== null
                          ? <span style={{ color: "var(--amber)", fontFamily: "var(--font-mono)", fontWeight: 600 }}>{fmtCost(m.cost)}</span>
                          : <span style={{ color: "var(--text-dim)", fontSize: 11 }}>包月</span>
                        }
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
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
    </div>
  );
}

function TokenRow({ label, value, raw, max, color }: {
  label: string; value: string; raw: number; max: number; color: string;
}) {
  return (
    <div style={{ marginBottom: "0.875rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 5 }}>
        <span style={{ color: "var(--text-muted)", fontSize: 12 }}>{label}</span>
        <span style={{ color, fontWeight: 700, fontSize: 13, fontFamily: "var(--font-mono)" }}>{value}</span>
      </div>
      <Bar value={raw} max={max} color={color} />
    </div>
  );
}

function DailyChart({ data }: { data: TokenDailyStat[] }) {
  const sorted = [...data].sort((a, b) => a.date.localeCompare(b.date));
  const W = 640, H = 180, PAD_L = 52, PAD_B = 28, PAD_T = 12, PAD_R = 12;
  const chartW = W - PAD_L - PAD_R;
  const chartH = H - PAD_T - PAD_B;
  const barW = Math.max(4, Math.floor(chartW / sorted.length) - 3);
  const step = chartW / sorted.length;

  const [mode, setMode] = useState<"token" | "cost">("token");
  const [hovered, setHovered] = useState<number | null>(null);

  const hasCost = sorted.some((d) => d.cost != null);
  const MODEL_PALETTE = ["var(--teal)", "var(--amber)", "var(--purple)", "var(--orange)", "var(--blue)"];
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

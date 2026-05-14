import type {
  NormalizedEvent,
  Project,
  SessionSummary,
} from "@/types/events";

// 使用相对路径 — Next.js rewrites 会代理到 api:8000
const BASE = "";

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const qs = params
    ? Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== null)
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
        .join("&")
    : "";
  const url = `${BASE}${path}${qs ? `?${qs}` : ""}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function post<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: "POST" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export interface ProjectOverview {
  project_id: string;
  display_name: string;
  total_sessions: number;
  today_sessions: number;
  today_calls: number;
  last_session_at: string | null;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost: number | null;
}

export interface TokenOverview {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  calls: number;
}

export interface TokenDailyStat {
  date: string;
  input_tokens: number;
  output_tokens: number;
  calls: number;
  cost: number | null;
  model_costs: { model: string; cost: number }[];
  model_tokens: { model: string; total_tokens: number }[];
}

export interface TokenByModel {
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  calls: number;
  cost: number | null;
}

export interface ToolStat {
  tool_name: string;
  calls: number;
}

export interface MhxyExecutorStatus {
  service: string;
  executor_url?: string;
  status: "healthy" | "unhealthy" | "stale" | "unknown" | string;
  checked_at?: string;
  app_health_checked_at?: string;
  stale?: boolean;
  age_sec?: number | null;
  consecutive_failures?: number;
  fail_threshold?: number;
  interval_sec?: number;
  error?: string;
  health?: {
    ok?: boolean;
    status_code?: number;
    latency_ms?: number;
    body?: string;
    error?: string;
  };
  app_health?: Array<{
    port?: string;
    healthy?: boolean;
    adb?: boolean;
    screenshot?: boolean;
    ocr?: boolean;
    latency_ms?: number;
    error?: string;
  }>;
  process?: {
    ok?: boolean;
    pid?: number;
    started_at?: string;
    working_set_bytes?: number;
    command_line?: string;
    error?: string;
  };
  last_restart?: {
    ok?: boolean;
    reason?: string;
    latency_ms?: number;
  } | null;
}

export interface RuntimeModelInfo {
  model_key: string;
  model: string;
  display_name: string;
  provider: string;
  updated_at: string;
}

export interface AvailableModelInfo {
  key: string;
  display_name: string;
  provider: string;
}

export interface ProjectRuntimeModels {
  project_id: string;
  text_model: RuntimeModelInfo | null;
  vl_model: RuntimeModelInfo | null;
  available_text_models: AvailableModelInfo[];
  available_vl_models: AvailableModelInfo[];
}

export interface MhxyInstanceDetail {
  port: string;
  school: string;
  role: "leader" | "member" | "standalone" | string;
  group_id: number | null;
  healthy: boolean | null;
  adb: boolean | null;
  screenshot: boolean | null;
  ocr: boolean | null;
  latency_ms: number | null;
  error: string | null;
}

export interface MhxyExecutorInstances {
  instances: MhxyInstanceDetail[];
  app_health_checked_at: string | null;
}

export const api = {
  projects: () => get<Project[]>("/api/projects"),
  overview: () => get<ProjectOverview[]>("/api/stats/overview"),

  sessions: (params: {
    project_id?: string;
    agent_id?: string;
    since?: string;
    until?: string;
    limit?: number;
    offset?: number;
  }) => get<SessionSummary[]>("/api/sessions", params),

  session: (id: string) => get<SessionSummary & { metadata: Record<string, unknown> }>(`/api/sessions/${id}`),
  timeline: (id: string) => get<{ events: NormalizedEvent[]; rounds_by_trace: Record<string, number> }>(`/api/sessions/${id}/timeline`),
  trace: (id: string) => get<{ trace_id: string; total_cost: number | null; events: NormalizedEvent[] }>(`/api/traces/${id}`),

  tokensOverview: (project_id?: string) =>
    get<TokenOverview>("/api/stats/tokens/overview", project_id ? { project_id } : undefined),
  tokensDaily: (project_id?: string, days = 14) =>
    get<TokenDailyStat[]>("/api/stats/tokens/daily", { ...(project_id ? { project_id } : {}), days }),
  tokensByModel: (project_id?: string) =>
    get<TokenByModel[]>("/api/stats/tokens/by-model", project_id ? { project_id } : undefined),
  tools: (project_id?: string) =>
    get<ToolStat[]>("/api/stats/tools", project_id ? { project_id } : undefined),
  mhxyExecutorStatus: () => get<MhxyExecutorStatus>("/api/external/mhxy-executor/status"),
  mhxyExecutorInstances: () => get<MhxyExecutorInstances>("/api/external/mhxy-executor/instances"),
  mhxyExecutorScreenshot: (port: string) =>
    get<{ port: string; image_b64: string }>("/api/external/mhxy-executor/screenshot", { port }),
  mhxyExecutorBatchTap: (ports: string[], px: number, py: number) =>
    postJson<{ results: Record<string, boolean> }>("/api/external/mhxy-executor/batch-tap", { ports, px, py }),
  mhxyExecutorBatchSwipe: (
    ports: string[],
    x1: number,
    y1: number,
    x2: number,
    y2: number,
    duration_ms = 300,
  ) =>
    postJson<{ results: Record<string, boolean> }>("/api/external/mhxy-executor/batch-swipe", { ports, x1, y1, x2, y2, duration_ms }),
  think: (params: { project_id?: string; session_id?: string; limit?: number }) =>
    get<NormalizedEvent[]>("/api/think", params),

  runtimeModels: () => get<ProjectRuntimeModels[]>("/api/projects/runtime-models"),

  ingestMhxy: () => post<{ status: string; events_inserted: number }>("/api/ingest/mhxy"),
  ingestMhxyExecutor: () => post<{ status: string; events_inserted: number }>("/api/ingest/mhxy-executor"),
  ingestStockBot: () => post<{ status: string; events_inserted: number }>("/api/ingest/stock-bot"),
  ingestEhsBot: () => post<{ status: string; events_inserted: number }>("/api/ingest/ehs-bot"),
};

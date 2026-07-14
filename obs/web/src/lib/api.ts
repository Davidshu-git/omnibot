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

async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : detail;
    } catch {
      // keep HTTP status fallback
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json();
}

export interface ProjectOverview {
  project_id: string;
  display_name: string;
  total_sessions: number;
  today_sessions: number;
  today_calls: number;
  last_session_at: string | null;
  last_session_id: string | null;
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
  daily_model: RuntimeModelInfo | null;
  available_text_models: AvailableModelInfo[];
  available_vl_models: AvailableModelInfo[];
  available_daily_models: AvailableModelInfo[];
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

export interface BotChatResult {
  reply: string;
  obs_session_id: string;
  trace_id: string;
}

export interface PortfolioHolding {
  ticker: string;
  company_name: string;
  shares: number;
  type?: string; // "stock" | "etf" | "crypto"，用于证券/加密分类
  current_price?: number;
  currency?: string;
  currency_symbol?: string;
  native_market_value?: number;
  market_value_cny?: number;
  cost_value_cny?: number;
  profit_loss_cny?: number;
  profit_loss_percent?: number;
  suspect?: boolean;
  error?: string;
}

export interface PortfolioCashHolding {
  platform: string;
  amount: number;
  currency: string;
  cny_value: number;
}

export interface FxTrend {
  rate: number;
  change_pct: number;
  window_days?: number;
  spark?: number[];
}

export interface PortfolioHistoryPoint {
  date: string;
  total_market_value: number;
  total_profit_loss: number;
  profit_loss_percent: number;
  securities_total_cny: number;
  cash_total_cny: number;
}

export interface PortfolioHistoryExcludedPoint {
  date: string;
  errored_tickers: string[];
}

export interface PortfolioHistoryResponse {
  points: PortfolioHistoryPoint[];
  excluded_points?: PortfolioHistoryExcludedPoint[];
  excluded_count?: number;
}

export interface PortfolioSnapshot {
  available: boolean;
  date?: string;
  generated_at?: string;
  currency_unit?: string;
  total_market_value?: number;
  total_cost?: number;
  total_profit_loss?: number;
  profit_loss_percent?: number;
  /** 累计已实现盈亏（平仓落袋，按快照当日汇率折 CNY）；旧快照无此字段。 */
  realized_pnl_total_cny?: number;
  realized_pnl_by_currency?: Record<string, number>;
  securities_total_cny?: number;
  crypto_total_cny?: number;
  cash_total_cny?: number;
  currency_exposure?: Record<string, number>;
  fx_trend?: Record<string, FxTrend>;
  has_pricing_error?: boolean;
  errored_tickers?: string[];
  holdings?: PortfolioHolding[];
  cash_holdings?: PortfolioCashHolding[];
  exchange_rates?: Record<string, number>;
}

export interface StockTrendPoint {
  date: string;
  close: number;
  ma20: number | null;
  ma60: number | null;
  ma250: number | null;
}

export interface StockTrendMaInfo {
  available: boolean;
  value?: number;
  direction?: "向上" | "向下" | "走平";
  slope_pct?: number;
  deviation_pct?: number;
  deviation_percentile?: number | null;
}

export interface StockTrendTrade {
  date: string;
  price: number;
  side: "buy" | "sell";
  details: string;
}

/** 基本面快照。后端 `.info` 端点整体降级——取不到时 fundamentals 为 null。
 * 每个字段本身也可能为 null（yfinance 该标的缺该项）。dividend_yield 是小数分数
 * （0.0052 = 0.52%），前端负责 ×100 呈现；crypto（is_crypto=true）只有市值 + 24h 量。 */
export interface StockFundamentals {
  is_crypto: boolean;
  currency?: string | null;
  market_cap?: number | null;
  // 证券字段
  pe_ttm?: number | null;
  /** 动态市盈率（基于未来盈利预测）；亏损股无 TTM 时的替代指标。 */
  forward_pe?: number | null;
  /** TTM 市盈率成因：ok=有效数值 / loss=当前亏损无意义 / missing=数据源暂缺。
   * 用于把「—」的原因显式化，区分「亏损」与「抓取不到」。 */
  pe_ttm_status?: "ok" | "loss" | "missing";
  pb?: number | null;
  dividend_yield?: number | null;
  week52_low?: number | null;
  week52_high?: number | null;
  // 加密货币字段
  volume_24h?: number | null;
}

export interface StockTrend {
  status: string;
  detail?: string;
  ticker?: string;
  /** 本次结果对应的显示窗口（6mo/1y/2y/5y/max）。 */
  period?: string;
  latest_price?: number;
  latest_date?: string;
  series?: StockTrendPoint[];
  ma20?: StockTrendMaInfo;
  ma60?: StockTrendMaInfo;
  ma250?: StockTrendMaInfo;
  regime_note?: string;
  trades?: StockTrendTrade[];
  /** 基本面快照；后端取不到时为 null（整块不渲染）。 */
  fundamentals?: StockFundamentals | null;
}

export interface ScreenerResult {
  ticker: string;
  name?: string | null;
  passed: true;
  latest_price: number;
  ma250_direction: "向上";
  relative_strength_pct: number | null;
  trend_duration_days: number;
  trend_duration_capped: boolean;
  deviation_percentile_ma60: number | null;
  tag?: string | null;
}

export interface ScreenerSkipped {
  ticker: string;
  name?: string | null;
  passed: false;
  skip_reason: string;
  tag?: string | null;
}

export interface ScreenerStatus {
  status: "idle" | "running" | "done" | "error";
  total?: number;
  done?: number;
  started_at?: string;
  completed_at?: string;
  results?: ScreenerResult[];
  skipped?: ScreenerSkipped[];
  passed_count?: number;
  error?: string;
}

export interface WatchlistItem {
  ticker: string;
  note: string;
  added_at: string;
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
mhxyExecutorStatus: () => get<MhxyExecutorStatus>("/api/external/mhxy-executor/status"),
  mhxyExecutorPower: (enabled: boolean) =>
    postJson<{ enabled: boolean; updated_at: string; source: string }>(
      "/api/external/mhxy-executor/power",
      { enabled },
    ),
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
  sendBotChat: (project: string, userId: number, text: string, signal?: AbortSignal) =>
    postJson<BotChatResult>(`/api/external/${project}/chat`, { user_id: userId, text }, signal),
  think: (params: { project_id?: string; session_id?: string; limit?: number }) =>
    get<NormalizedEvent[]>("/api/think", params),

  runtimeModels: () => get<ProjectRuntimeModels[]>("/api/projects/runtime-models"),

  switchModel: (project: string, kind: "text" | "vl" | "daily", modelKey: string) =>
    postJson<{ kind: string; model_key: string; display_name: string }>(
      `/api/external/${project}/switch-model`,
      { kind, model_key: modelKey },
    ),

  ingestMhxy: () => post<{ status: string; events_inserted: number }>("/api/ingest/mhxy"),
  ingestMhxyExecutor: () => post<{ status: string; events_inserted: number }>("/api/ingest/mhxy-executor"),
  ingestStockBot: () => post<{ status: string; events_inserted: number }>("/api/ingest/stock-bot"),
  ingestEhsBot: () => post<{ status: string; events_inserted: number }>("/api/ingest/ehs-bot"),

  portfolioLatest: () => get<PortfolioSnapshot>("/api/portfolio/latest"),
  portfolioHistory: (days = 90) =>
    get<PortfolioHistoryResponse>("/api/portfolio/history", { days }),
  // 触发 stock bot 重新取价 + 覆盖当天快照（区别于 portfolioLatest 的纯重读）。
  portfolioRefresh: () =>
    post<{ status: string; generated_at?: string; total_market_value?: number; retry_after?: number; detail?: string }>(
      "/api/external/stock-bot/refresh-portfolio",
    ),
  // 个股趋势分析（价格 + MA20/60/250）：仅 stock bot 支持，硬编码 project。
  // period 为显示窗口（6mo/1y/2y/5y/max，白名单在估值引擎 TREND_WINDOWS）。
  stockTrend: (ticker: string, period: string) =>
    postJson<StockTrend>("/api/external/stock-bot/stock-trend", { ticker, period }),

  // 选股筛股：仅 stock bot 支持，硬编码 project。
  screenerUniverse: () => post<{ tickers: string[] }>("/api/external/stock-bot/screener-universe"),
  screenerUniverseSave: (tickers: string[]) =>
    postJson<{ tickers: string[] }>("/api/external/stock-bot/screener-universe-save", { tickers }),
  screenerStart: () => post<{ status: string }>("/api/external/stock-bot/screener-start"),
  screenerStatus: () => post<ScreenerStatus>("/api/external/stock-bot/screener-status"),
  // 随代码库打包的预置美股股票池（静态资源，非用户自己保存的 universe）。
  screenerPreset: () => post<{ tickers: string[] }>("/api/external/stock-bot/screener-preset"),

  // 自选观察清单：读走 obs 直读挂载文件（秒回、不依赖 live bot）；增删走 live bot 代理。
  watchlist: () => get<{ items: WatchlistItem[] }>("/api/portfolio/watchlist"),
  watchlistAdd: (ticker: string, note = "") =>
    postJson<{ status: string; items: WatchlistItem[] }>(
      "/api/external/stock-bot/watchlist-add",
      { ticker, note },
    ),
  watchlistRemove: (ticker: string) =>
    postJson<{ status: string; items: WatchlistItem[] }>(
      "/api/external/stock-bot/watchlist-remove",
      { ticker },
    ),
};

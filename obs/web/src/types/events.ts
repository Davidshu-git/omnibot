/**
 * TypeScript mirror of the unified event schema (app/schemas/events.py).
 * All frontend code must use these types — never the raw mhxy format.
 */

export type EventType =
  | "session_started"
  | "session_ended"
  | "message"
  | "thought"
  | "model_call"
  | "tool_call"
  | "tool_result"
  | "metric"
  | "event"
  | "error";

export type ThoughtKind = "reasoning_summary" | "custom_think" | "extracted";

export interface SessionStartedPayload {
  channel?: string;
  title?: string;
}

export interface SessionEndedPayload {
  status: "success" | "failed" | "interrupted" | "unknown";
}

export interface MessagePayload {
  role: "user" | "assistant" | "system" | "tool";
  content: string;
}

export interface ThoughtPayload {
  kind: ThoughtKind;
  provider: string;
  content: string;
  summary_level: "brief" | "detailed" | "unknown";
}

export interface ModelCallPayload {
  provider?: string;
  model?: string;
  prompt?: string;
  raw_output?: string;
  input_tokens?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  cache_read_tokens?: number;
  duration_ms?: number;
  success: boolean;
}

export interface ToolCallPayload {
  tool_name: string;
  arguments?: unknown;
}

export interface ToolResultPayload {
  tool_name: string;
  success: boolean;
  result?: unknown;
  duration_ms?: number;
}

export interface MetricPayload {
  metric_name: string;
  metric_value: number;
  metric_unit?: string;
}

export interface CustomEventPayload {
  name: string;
  payload?: unknown;
}

export interface ErrorPayload {
  name: string;
  message: string;
  stack?: string;
  severity: "warning" | "error" | "critical";
}

export interface NormalizedEvent {
  event_id: string;
  project_id: string;
  agent_id?: string;
  session_id: string;
  trace_id?: string;
  run_id?: string;
  event_type: EventType;
  timestamp: string; // ISO 8601
  source: string;
  payload: Record<string, unknown>;
  extra: Record<string, unknown>;
}

export interface SessionSummary {
  id: string;
  project_id: string;
  agent_id?: string;
  started_at?: string;
  ended_at?: string;
  status: string;
}

export interface Project {
  id: string;
  display_name: string;
  source_type: string;
  created_at: string;
}

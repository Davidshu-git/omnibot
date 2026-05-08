# Schema

## 1. 目标

定义平台内部统一事件模型，用于：

- 多数据源接入
- 查询与聚合
- 前端统一展示

外部项目不强制原生输出此 schema，但所有 adapter 最终必须映射到该模型。

---

## 2. 统一标识字段

每个标准化事件必须具备：

- `event_id`
- `project_id`
- `agent_id`
- `session_id`
- `trace_id`
- `run_id`
- `event_type`
- `timestamp`
- `source`
- `extra`

### 字段定义

#### `event_id`
- 平台内部唯一事件 ID

#### `project_id`
- 业务项目 ID

#### `agent_id`
- 智能体 ID

#### `session_id`
- 业务会话 ID

#### `trace_id`
- 执行链路 ID

#### `run_id`
- 单次运行 ID

#### `event_type`
- 事件主类型

#### `timestamp`
- 事件发生时间

#### `source`
- 数据来源

#### `extra`
- 扩展字段

---

## 3. 事件类型

支持的一级事件类型：

- `session_started`
- `session_ended`
- `message`
- `thought`
- `model_call`
- `tool_call`
- `tool_result`
- `metric`
- `event`
- `error`

---

## 4. 事件结构

统一结构建议：

```json
{
  "event_id": "evt_xxx",
  "project_id": "mhxy",
  "agent_id": "game-bot",
  "session_id": "tg_123_20260419",
  "trace_id": "trace_xxx",
  "run_id": "run_xxx",
  "event_type": "tool_call",
  "timestamp": "2026-04-19T08:00:00+08:00",
  "source": "mhxy_jsonl",
  "payload": {},
  "extra": {}
}
```

---

## 5. 事件 payload 规范

### 5.1 `session_started`

```json
{
  "channel": "telegram",
  "title": "optional"
}
```

### 5.2 `session_ended`

```json
{
  "status": "success | failed | interrupted | unknown"
}
```

### 5.3 `message`

```json
{
  "role": "user | assistant | system | tool",
  "content": "..."
}
```

### 5.4 `thought`

```json
{
  "kind": "reasoning_summary | custom_think | extracted",
  "provider": "openai | dashscope | custom",
  "content": "...",
  "summary_level": "brief | detailed | unknown"
}
```

### 5.5 `model_call`

```json
{
  "provider": "openai",
  "model": "gpt-5.4",
  "prompt": "...",
  "raw_output": "...",
  "input_tokens": 100,
  "output_tokens": 80,
  "reasoning_tokens": 20,
  "cache_read_tokens": 0,
  "duration_ms": 1200,
  "success": true
}
```

### 5.6 `tool_call`

```json
{
  "tool_name": "sense_screen",
  "arguments": {
    "port": "5557"
  }
}
```

### 5.7 `tool_result`

```json
{
  "tool_name": "sense_screen",
  "success": true,
  "result": "识别到 5 条文字",
  "duration_ms": 350
}
```

### 5.8 `metric`

```json
{
  "metric_name": "retry_count",
  "metric_value": 2,
  "metric_unit": "count"
}
```

### 5.9 `event`

```json
{
  "name": "task_stage_changed",
  "payload": {
    "from": "planning",
    "to": "executing"
  }
}
```

### 5.10 `error`

```json
{
  "name": "tool_execution_error",
  "message": "xxx",
  "stack": "optional",
  "severity": "warning | error | critical"
}
```

---

## 6. 数据分层

### 6.1 原始数据

建议保存在 `raw_event_blobs`

字段：
- `id`
- `project_id`
- `source`
- `external_key`
- `collected_at`
- `payload_json`
- `payload_hash`

### 6.2 标准化事件

建议保存在 `events`

字段：
- `id`
- `project_id`
- `agent_id`
- `session_id`
- `trace_id`
- `run_id`
- `event_type`
- `timestamp`
- `source`
- `payload_json`

### 6.3 聚合结果

建议保存在：
- `daily_usage_stats`

---

## 7. 幂等键设计

为了支持增量同步与重放，必须设计稳定幂等键。

建议组合：

- `source`
- `project_id`
- `external_key`
- `payload_hash`

示例：

### JSONL
- `external_key = file_path + line_number`

### LangSmith
- `external_key = run_id` 或 `trace_id + run_id`

---

## 8. 查询模型要求

必须支持：

- 按项目列会话
- 按 Agent 列会话
- 单会话时间线
- 单 trace 详情
- token 统计
- tool 聚合
- think 搜索

建议索引：

- `events(project_id, timestamp)`
- `events(session_id, timestamp)`
- `events(trace_id, timestamp)`
- `events(event_type, timestamp)`
- `sessions(project_id, started_at desc)`
- `sessions(agent_id, started_at desc)`

---

## 9. Session / Trace / Run 关系

必须明确：

- 一个 `session` 可以包含多个 `trace`
- 一个 `trace` 可以包含多个 `run`
- 前端主导航按 `session` 展开
- trace 是 session 下的执行链路视图

禁止把 `trace` 直接当成 `session`。

---

## 10. Adapter 映射要求

每个 adapter 必须输出：

- 规范化后的 `project_id`
- 规范化后的 `agent_id`
- 稳定的 `session_id`
- 可选 `trace_id`
- 可选 `run_id`
- 事件类型与 payload

如果上游没有某字段：
- 可为空
- 但必须在 adapter 文档中说明生成规则

---

## 11. 第一版强制约束

第一版实施必须遵守：

1. 所有事件必须可映射为统一 schema
2. 所有数据源必须可幂等重跑
3. 所有展示页只读，不反向修改源数据
4. 项目专属字段一律放入 `extra`

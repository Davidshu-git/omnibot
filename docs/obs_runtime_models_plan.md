# 方案：obs 总览页展示各 Bot 当前使用的模型（含视觉模型）

## 背景

`obs` 的总览页（`/web/src/pages/index.tsx`）展示每个 Bot 的会话数 / Token /
费用估算，但**看不到这个 Bot 当前正在用哪个文本模型 / 视觉模型**。

各 Bot 支持 Telegram 命令热切换模型（`/model`、`/vlmodel`），切换状态写入：
- 文本模型：`data/{bot}/model_settings.json`（仅含 `{"model_key": "qwen"}`）
- 视觉模型：`data/{bot}/vl_model_settings.json`（仅 `mhxy_bot` 有，其余两 Bot 无视觉模型）

obs 目前完全不感知这些文件，只能从 `events.payload_json["model"]` 反推
"最近一次实际调用的模型"——这与"当前已配置但尚未触发"的状态可能不一致。

目标：让 obs 总览页每张 Bot 卡片直接显示当前配置的文本模型 + 视觉模型，
信息源是"配置态"而非"已用态"。

---

## 设计决策

### 1. 数据源：扩展现有 settings JSON，不新增表

不在 PostgreSQL 建表。原因：
- 模型选择是低频事件，每次切换才写一次，没必要走 ingest 流水线。
- "当前选择"是单值状态，KV 文件足够，进 PG 反而要做 upsert。

### 2. settings JSON 字段升级（关键）

现有文件只存 `model_key`，obs 拿不到 display_name/model/provider。
**让 Bot 在写入时把"已解析"的完整信息一并落盘**，obs 直接读，避免 obs 复制一份模型目录。

升级后的 `model_settings.json`：
```json
{
  "model_key": "qwen",
  "model": "qwen3.5-plus",
  "display_name": "Qwen 3.5 Plus",
  "provider": "dashscope",
  "updated_at": "2026-05-08T12:00:00+08:00"
}
```

`vl_model_settings.json` 同结构（`provider` 视情况省略或固定 `dashscope`）。

`provider` 由 `base_url` 推导：
- `dashscope.aliyuncs.com` → `dashscope`
- `minimaxi.com` → `minimax`
- `deepseek.com` → `deepseek`
- 其他 → `unknown`

### 3. 不引入新通道，复用 observability 目录的卷挂载

把 runtime 状态文件路径从 `data/{bot}/model_settings.json` **不要搬动**
（兼容 Bot 现有读写代码），而是改 `obs` 的卷挂载，使 obs 能看到它们。

obs 当前挂载：`data/{bot}/observability/sessions/` → `/logs/{bot}/sessions`。
**新增**专用挂载：`data/{bot}/` → `/runtime/{bot}` 只读，仅供 obs 读取
两个 settings JSON。

> 备选：把 settings 文件**写入**`observability/` 子目录（已挂载），不改 compose。
> 否决：会破坏 Bot 当前对路径的硬编码假设，迁移成本更高。

---

## 改动范围

### Bot 端（共 3 文件）

#### 1. `core/model_registry.py`

修改 `ModelRegistry._save_key()` 与 `_load_key()`：

```python
def _save_key(self, key: str) -> None:
    cfg = self._configs[key]
    payload = {
        "model_key": key,
        "model": cfg.model,
        "display_name": cfg.display_name,
        "provider": _provider_from_base_url(cfg.base_url),
        "updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
    }
    self._settings_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
```

`_load_key` 兼容旧格式：只读 `model_key` 字段，其他字段忽略。

新增模块级辅助函数：
```python
def _provider_from_base_url(url: str) -> str:
    if "dashscope" in url: return "dashscope"
    if "minimax" in url:   return "minimax"
    if "deepseek" in url:  return "deepseek"
    return "unknown"
```

**重要**：在 `__init__` 末尾，如果 `_settings_path` 不存在或字段缺失，**主动调用一次 `_save_key(self._current_key)`** 把完整信息落盘。
保证 Bot 启动后，obs 立刻能看到当前状态，不必等用户切换。

#### 2. `core/vl_model_registry.py`

同样的扩展。`VlModelConfig` 没有 `base_url`，写入时省略 `provider` 字段
或固定写 `"dashscope"`（mhxy 的 VL 模型当前都走 DashScope）。

#### 3. *无需改 `mhxy_bot/agent.py` / `stock_bot/agent.py` / `ehs_bot/agent.py`*
扩展是注册中心内部行为，调用方零变更。

---

### obs API 端（2 文件）

#### 1. `obs/docker-compose.yml`

`api` 服务新增 3 个只读挂载（替换或补充现有 `observability/sessions` 挂载）：

```yaml
volumes:
  - ./api:/app
  - ${MHXY_HOST_LOG_DIR:-...}/observability/sessions:/logs/mhxy/sessions:ro
  - ${MHXY_HOST_DATA_DIR:-/volume1/server/.openclaw/workspace/projects/omnibot/data/mhxy}:/runtime/mhxy:ro
  - ${OMNIBOT_STOCK_HOST_DATA_DIR:-/volume1/server/.openclaw/workspace/projects/omnibot/data/stock}:/runtime/stock-bot:ro
  - ${OMNIBOT_EHS_HOST_DATA_DIR:-/volume1/server/.openclaw/workspace/projects/omnibot/data/ehs}:/runtime/ehs-bot:ro
  # 现有的 sessions 挂载和 mhxy executor 挂载保持不变
```

加新环境变量：
```yaml
environment:
  RUNTIME_DIR_MHXY: /runtime/mhxy
  RUNTIME_DIR_STOCK_BOT: /runtime/stock-bot
  RUNTIME_DIR_EHS_BOT: /runtime/ehs-bot
```

更新 `obs/.env.example`（如果有）补充三条 `*_HOST_DATA_DIR`。

#### 2. `obs/api/app/api/router.py` 新增端点

新增 `GET /api/projects/runtime-models`：

```python
@router.get("/projects/runtime-models")
async def projects_runtime_models():
    """读取每个 project 的 model_settings.json / vl_model_settings.json，
    返回当前配置的文本模型 / 视觉模型。"""
    runtime_dirs = {
        "mhxy": os.getenv("RUNTIME_DIR_MHXY"),
        "stock-bot": os.getenv("RUNTIME_DIR_STOCK_BOT"),
        "ehs-bot": os.getenv("RUNTIME_DIR_EHS_BOT"),
    }
    out = []
    for project_id, base in runtime_dirs.items():
        if not base:
            continue
        text = _read_settings_safe(Path(base) / "model_settings.json")
        vl = _read_settings_safe(Path(base) / "vl_model_settings.json")
        out.append({
            "project_id": project_id,
            "text_model": text,    # {model_key, model, display_name, provider, updated_at} 或 None
            "vl_model": vl,         # 同上，仅 mhxy 非空
        })
    return out
```

`_read_settings_safe` 容错：文件不存在 / JSON 损坏 / 字段缺失，返回 `None`。
**绝不抛异常导致整个端点 5xx**。

为什么不挤进 `/api/stats/overview`：
- overview 走重型 SQL 聚合，加一次磁盘 I/O 会拖慢响应。
- runtime 单独刷新，前端可独立轮询、独立处理 loading 状态。

---

### obs 前端（3 文件）

#### 1. `obs/web/src/lib/api.ts`

新增类型与方法：

```typescript
export interface RuntimeModelInfo {
  model_key: string;
  model: string;
  display_name: string;
  provider: string;
  updated_at: string;
}

export interface ProjectRuntimeModels {
  project_id: string;
  text_model: RuntimeModelInfo | null;
  vl_model: RuntimeModelInfo | null;
}

// 在 api 对象中新增：
runtimeModels: () => get<ProjectRuntimeModels[]>("/api/projects/runtime-models"),
```

#### 2. `obs/web/src/pages/index.tsx`

- 新增 state：`const [runtime, setRuntime] = useState<ProjectRuntimeModels[]>([]);`
- `load()` 内并行 `api.runtimeModels()`，存入 state。
- 把 `runtime` 按 `project_id` 索引成 Map，传给 `ProjectCard`。
- 与 `executor_status` 类似，**SSE 收到 `model_switched` 事件时刷新**（见下）。

`ProjectCard` 新增渲染区域，放在"今日调用 Stat"和"token bar"之间：

```tsx
{(rt?.text_model || rt?.vl_model) && (
  <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: "1rem" }}>
    {rt.text_model && (
      <ModelPill icon="🧠" label={rt.text_model.display_name}
                 sub={rt.text_model.provider} />
    )}
    {rt.vl_model && (
      <ModelPill icon="👁️" label={rt.vl_model.display_name}
                 sub={rt.vl_model.provider} />
    )}
  </div>
)}
```

`ModelPill` 新增小组件（同文件即可）：圆角 badge，icon + display_name + 灰色小字 provider。

#### 3. SSE 增强（可选，建议做）

`obs/api/app/api/router.py` 已有 `/api/stream`。在 ingest 完成时广播 `executor_status`。
**新增**：检测 `model_settings.json` mtime 变化时广播 `model_switched` 事件。

最简实现：API 收到 `/projects/runtime-models` 请求时记录当前 mtime；下次请求若 mtime 变了，下一帧 SSE 推一条 `model_switched`。

> **如果嫌麻烦，可省略 SSE 部分**：前端用 30s 轮询同样能拿到最新状态。
> 推荐先不做 SSE，第一版完成后再迭代。

---

## 安全与边界

- **不暴露 `api_key` 字段**。`ModelConfig.api_key` 永远不写入 `model_settings.json`，
  obs 也不读取（即便读到也忽略）。代码里加注释强调。
- **挂载是只读**（`:ro`），obs 没有写入风险。
- 文件不存在 / 损坏时端点返回 `null`，前端按"未配置"展示而非报错。
- `provider` 字段为兜底信息，不参与计费或路由，纯展示用。

---

## 验证方式

### 单元
- `core/model_registry.py`：mock settings 文件，断言 `_save_key("minimax")` 写入的 JSON 含全部 5 个字段，`provider == "minimax"`。
- 旧格式兼容：写一个仅含 `{"model_key": "qwen"}` 的文件，`_load_key()` 仍返回 `"qwen"`。

### 集成
1. 启动各 Bot，确认 `data/{bot}/model_settings.json` 自动升级为新格式。
2. `curl http://localhost:8000/api/projects/runtime-models` 返回三个 project，
   `mhxy` 有 `text_model` 和 `vl_model`，其余两个 `vl_model = null`。
3. Telegram 中执行 `/model deepseek`，文件 mtime 变化，再次 curl 返回 `display_name: "DeepSeek V4 Flash"`。
4. 重启 obs：`docker compose -f obs/docker-compose.yml restart api`，确认 mount 生效。
5. 浏览器打开 `http://localhost:3100/`，每张卡片显示模型 pill。

---

## 实施顺序（建议给 codex）

1. 先做 Bot 侧（`core/model_registry.py` + `core/vl_model_registry.py`），跑单元测试，重启 Bot 确认 JSON 格式升级且无回归。
2. 改 `obs/docker-compose.yml`，加 runtime 挂载，`docker compose up -d` 验证 `/runtime/{bot}/model_settings.json` 在容器内可读。
3. 加 `/api/projects/runtime-models` 端点，curl 验证。
4. 前端类型 + ProjectCard pill，浏览器验证。
5. （可选）SSE 推送切换事件。

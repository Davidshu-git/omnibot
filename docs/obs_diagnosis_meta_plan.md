# 方案：将 InstanceDiagnosis 提升为 obs 一等公民

## 背景

`runner_step_log_plan.md` 已落地：`InstanceDiagnosis.steps` 字段
被 `check_instance_health` 拼成 `【诊断过程】…` 段落塞进工具返回字符串。

LLM 端拿到了诊断推理链，但 obs 平台**完全没有受益**：

- `OmnibotObsCallbackHandler.on_tool_end` 把工具返回值整体 truncate 到
  4096 字符（`core/observability.py:386-400`），作为 `tool_result.output` 写入 JSONL。
- `omnibot_jsonl.py` 与 `mhxy_jsonl.py` 两个 adapter 把 `output` 原封不动
  塞进 `payload_json.result`，是一整团非结构化字符串。
- obs/web `sessions.tsx:266-294` 只能按字符切片做"展开/收起"渲染，无法
  按 `code` / `state` 检索、按失败模式聚合、按 step 文案做频次统计。

**目标**：把 `InstanceDiagnosis.as_dict()` 全部字段作为结构化 meta
透传到 obs，让诊断结果可检索、可聚合、可做趋势看板。

---

## 设计决策

### 1. 副信道实现路径（已 spike 验证）

排除掉的方案：

- **改 `@tool` 返回值类型**：LangChain 期望 `str`，改成 `tuple[str, dict]`
  会传染整个 agent 调度链，影响 LLM 可见字符串。
- **在返回字符串里嵌 `<obs-meta>{...}</obs-meta>` 标签**：脏字符串、
  容易被 LLM 当成正文复读、需要在多处做正则剥离。
- **adapter 反向解析 `【诊断过程】` 段落**：脆弱，文案任何变动都会让
  obs 静默丢字段，方向反了。
- ❌ **自建 `contextvars.ContextVar`**：spike 实测在 LangChain 0.3.84
  下完全不可用——callback handler 的 `on_tool_start` 与 `@tool` 函数
  跑在不同的 context 中（LangChain 用 `copy_context().run()` 包装回调），
  双向都看不到对方的 set。
- ❌ **`@tool` 函数声明 `run_manager` 参数**：LangChain 0.3.x 已不再
  自动注入此参数，反而会被解析进 args schema 暴露给 LLM。
- ⚠️ **subclass `BaseTool` 拿 `run_manager`**：spike 验证可行，但要把
  现有所有 `@tool` 装饰函数改成 class，侵入面过大；作为兜底备案。

✅ **采用方案**：利用 LangChain **内部** ContextVar
`langchain_core.runnables.config.var_child_runnable_config` —— 它由
LangChain 框架在进入 `@tool` 函数前 set，对工具函数可见，含 `callbacks`
列表。在工具函数里调 `attach_tool_meta(meta)` 时：

1. 从 `var_child_runnable_config.get()` 拿到当前 RunnableConfig；
2. 在 `cfg["callbacks"]` 列表里反查 `OmnibotObsCallbackHandler` 实例；
3. 直接调 `handler._set_pending_meta(meta)` 把 meta 挂到 handler 实例上；
4. handler 自己的 `on_tool_end` 读取 pending slot，写入 JSONL，再清空。

spike 已对 sequential 工具调用验证通过。

理由：

- 工具函数显式 opt-in，不写就走原路径，零回归风险。
- 不依赖任何 `@tool` 装饰器内部行为，对未来 LangChain 版本升级稳健。
- 工具返回字符串完全不变，LLM 行为零影响，与 v1 方案完全正交。

### ⚠️ 已知约束：sequential 工具执行假设

spike 实测：当两个工具**通过 `asyncio.gather` 并发** 调用时，handler
的单 pending slot 会被覆盖，第二个 attach 抢占第一个。

omnibot 当前所有 agent 默认走 LangChain `AgentExecutor`，**串行**执行
工具调用（即便 LLM 一次返回多个 tool_calls 也是 for 循环挨个 await），
本方案在生产环境**完全安全**。

如果未来开启 LangChain 的 parallel tool calling（需显式配置
`tools_executor` 走 `asyncio.gather`），则需把 `_pending_meta` 改造成
`dict[run_id, meta]`，并在 `on_tool_start` / `on_tool_end` 用 `kwargs["run_id"]` 索引。
当前不做。

### 2. JSONL `tool_result` 增加 `meta: dict | None` 字段

向后兼容：旧记录无 `meta` → adapter 当 `None` 处理，不报错。

### 3. obs 端结构化下沉到 `Event.payload_json.meta`

`payload_json` 是 JSONB，**无需 Alembic 迁移**。索引按需后加
（如 `(payload_json->'meta'->>'diagnosis_code')` GIN 索引）。

### 4. 通用而非 mhxy 专属

字段名 `meta` 而非 `diagnosis`：未来 stock_bot / ehs_bot 的工具
（如 `get_stock_quote`、`check_chemical_ghs`）也能复用同一通道带出
结构化数据（最近一次行情时间戳、化学品 CAS 号等）。诊断只是首个用例。

---

## 改动范围（5 个文件，1 个新增页面）

### 1. `core/observability.py` — 副信道与回调

新增公开 API：

```python
import json
import logging
log = logging.getLogger(__name__)

from langchain_core.runnables.config import var_child_runnable_config

_META_BYTE_LIMIT = 4096


def attach_tool_meta(meta: dict) -> bool:
    """供 @tool 函数调用：把结构化 meta 挂到本次 tool_result 事件。

    实现：从 LangChain 内部 RunnableConfig ContextVar 反查
    OmnibotObsCallbackHandler 实例，调其 _set_pending_meta()。

    返回 True 代表挂载成功；False 代表当前不在 obs 观测的工具上下文中
    （例：单跑、test 环境），调用方可忽略返回值。
    """
    cfg = var_child_runnable_config.get()
    if not cfg:
        return False
    cbs = cfg.get("callbacks")
    if cbs is None:
        return False

    if hasattr(cbs, "handlers"):
        handlers = list(cbs.handlers) + list(getattr(cbs, "inheritable_handlers", []))
    elif isinstance(cbs, list):
        handlers = list(cbs)
    else:
        return False

    target = next(
        (h for h in handlers if isinstance(h, OmnibotObsCallbackHandler)),
        None,
    )
    if target is None:
        return False

    target._set_pending_meta(meta)
    return True
```

`OmniObserver.log_tool_result` 签名加 `meta: dict | None = None` 参数，
非空时写入记录：

```python
if meta is not None:
    rec["meta"] = meta
```

`OmnibotObsCallbackHandler` 改造：

```python
def __init__(self, observer, trace_id, provider="dashscope"):
    ...
    self._pending_meta: dict | None = None    # 单 slot，sequential 假设

def _set_pending_meta(self, meta: dict) -> None:
    """attach_tool_meta() 通过此方法注入；4 KB 上限。"""
    try:
        s = json.dumps(meta, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        log.warning("attach_tool_meta: 不可序列化，丢弃: %s", exc)
        return
    if len(s.encode("utf-8")) > _META_BYTE_LIMIT:
        log.warning("attach_tool_meta: 超过 %d 字节，丢弃", _META_BYTE_LIMIT)
        return
    self._pending_meta = dict(meta)             # 拷贝防引用泄漏

async def on_tool_start(self, serialized, input_str, **kwargs):
    ...
    self._pending_meta = None                   # 每次工具调用前清空

async def on_tool_end(self, output, **kwargs):
    ...
    meta = self._pending_meta
    self._pending_meta = None
    self._obs.log_tool_result(..., meta=meta)

async def on_tool_error(self, error, **kwargs):
    ...
    meta = self._pending_meta                   # 错误路径也带出 meta
    self._pending_meta = None
    self._obs.log_tool_result(..., meta=meta)
```

### 2. `mhxy_bot/tools/game_tools.py` — 工具侧 opt-in

`check_instance_health` 在拼接返回字符串前加一行：

```python
diag = diagnose_instance(ctx)
d = diag.as_dict()

from core.observability import attach_tool_meta
attach_tool_meta({
    "kind": "instance_diagnosis",
    "port": _port_to_str(port),
    "code": d["code"],
    "state": d["state"],
    "needs_human": d["needs_human"],
    "steps": d["steps"],
    # message/details 已在 output 字符串里，不重复写 meta
})

# ...原有 prefix / steps_section / return 逻辑保持不变...
```

`batch_check_all_instances` **不动**——批量摘要的结构化值不大，
留给逐个详情页查阅即可。

### 3. `obs/api/app/adapters/omnibot_jsonl.py` 与 `mhxy_jsonl.py`

两个文件都需要：

1. 文件头 docstring 中 `tool_result` 字段定义同步加上 `meta?`：
   ```
   tool_result {type, timestamp, tool_name, output, success, duration_ms,
                error_message, meta?, trace_id?, run_id?}
   ```
2. `_map_record` 中 `tool_result` 分支加一行透传：

```python
if rtype == "tool_result":
    payload = {
        "tool_name": record.get("tool_name", ""),
        "success": record.get("success", True),
        "result": record.get("output"),
        "duration_ms": record.get("duration_ms"),
    }
    if record.get("meta") is not None:
        payload["meta"] = record["meta"]
    return NormalizedEvent(
        **base,
        event_type=EventType.TOOL_RESULT,
        payload=payload,
        extra=...,
    )
```

两个 adapter 同步改，**否则 mhxy 项目的诊断 meta 会静默丢失**
（obs/CLAUDE.md 已强调字段契约的对称性）。

### 4. `obs/api/app/api/router.py` — 新增聚合端点

需补 import：现有 `from sqlalchemy import func, select, distinct, String`
追加 `case`：

```python
from sqlalchemy import func, select, distinct, String, case
```

```python
@router.get("/stats/diagnoses")
async def diagnoses_stats(
    project_id: Optional[str] = Query("mhxy"),
    since: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """按 code / state 维度聚合诊断结果，支持失败率趋势。"""
    meta_kind = Event.payload_json["meta"]["kind"].as_string()
    code_col  = Event.payload_json["meta"]["code"].as_string().label("code")
    state_col = Event.payload_json["meta"]["state"].as_string().label("state")
    needs_h   = Event.payload_json["meta"]["needs_human"].as_boolean()

    base_filter = [
        Event.event_type == "tool_result",
        meta_kind == "instance_diagnosis",
    ]
    if project_id:
        base_filter.append(Event.project_id == project_id)
    if since:
        base_filter.append(Event.timestamp >= since)

    # 维度 1：code × state 分布
    by_code_q = (
        select(code_col, state_col, func.count().label("n"),
               func.sum(case((needs_h, 1), else_=0)).label("needs_human"))
        .where(*base_filter)
        .group_by(code_col, state_col)
        .order_by(func.count().desc())
    )
    rows = (await db.execute(by_code_q)).all()
    return [
        {"code": r.code, "state": r.state, "count": r.n,
         "needs_human": int(r.needs_human or 0)}
        for r in rows
    ]


@router.get("/stats/diagnoses/recent")
async def diagnoses_recent(
    project_id: str = Query("mhxy"),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """最近 N 次诊断详情，含完整 steps，用于前端 drill-down。"""
    meta_kind = Event.payload_json["meta"]["kind"].as_string()
    q = (
        select(Event)
        .where(
            Event.event_type == "tool_result",
            meta_kind == "instance_diagnosis",
            Event.project_id == project_id,
        )
        .order_by(Event.timestamp.desc())
        .limit(limit)
    )
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "event_id": e.event_id,
            "session_id": e.session_id,
            "timestamp": e.timestamp,
            "trace_id": e.trace_id,
            "meta": (e.payload_json or {}).get("meta") or {},
        }
        for e in rows
    ]
```

> **注意**：JSONB 路径表达式 `payload_json["meta"]["kind"]` 在不存在时
> 返回 SQL `NULL`，不会抛异常——无需先做 `IS NOT NULL` 守卫。

### 5. `obs/web/src/pages/sessions.tsx` — 结构化渲染

> ⚠️ 项目目前 `obs/web/src/components/` 只有 `CopyableId.tsx` / `Layout.tsx`
> / `Skeleton.tsx`，**没有 Chip 组件**。下方代码用 inline span 直接渲染，
> 不引入新组件。

`tool_result` 分支增加一段：当 `p.meta?.kind === "instance_diagnosis"` 时
渲染结构化标签与 steps 列表：

```tsx
{p.meta?.kind === "instance_diagnosis" && (
  <div style={{ marginTop: 6 }}>
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      <span style={chipStyle("muted")}>端口 {p.meta.port}</span>
      <span style={chipStyle(p.meta.code === "unknown_ok" ? "ok" : "warn")}>
        {p.meta.code}
      </span>
      <span style={chipStyle("muted")}>{p.meta.state}</span>
      {p.meta.needs_human && <span style={chipStyle("error")}>需人工</span>}
    </div>
    {p.meta.steps?.length > 0 && (
      <ol style={{ marginTop: 6, paddingLeft: 18, color: "var(--text-muted)", fontSize: 11 }}>
        {p.meta.steps.map((s: string, i: number) => <li key={i}>{s}</li>)}
      </ol>
    )}
  </div>
)}
```

`chipStyle` 在文件顶部加一个本地 helper：

```tsx
const chipStyle = (tone: "ok" | "warn" | "error" | "muted"): React.CSSProperties => ({
  display: "inline-block",
  padding: "1px 6px",
  fontSize: 10,
  fontFamily: "var(--font-mono)",
  borderRadius: 3,
  border: "1px solid var(--border)",
  color: {
    ok: "var(--green)", warn: "var(--orange)",
    error: "var(--red)", muted: "var(--text-muted)",
  }[tone],
});
```

字段不存在时分支自动跳过，对其他工具零影响。

### 6. （可选）`obs/web/src/pages/diagnoses.tsx` 新增汇总页

调 `/stats/diagnoses` 画 code × state 的热力表，
点击单元格跳 `/stats/diagnoses/recent` 列出最近条目，再点条目跳 session
timeline。属于第二阶段，可独立后做。

---

## 落地分阶段

| 阶段 | 改动 | 价值 |
|------|------|------|
| **P0**（必做） | 改动 1 + 2 + 3 | 诊断结构化进库，存量数据可回溯查询（payload JSONB） |
| **P1** | 改动 4 + 5 | 现有 sessions 页就能看到结构化 chip + steps 列表 |
| **P2**（可选） | 改动 6 | 独立汇总页，做失败模式与趋势 |

P0 是真正解决「字符串黑盒」的关键，P1/P2 是增益。

---

## 约束（强制执行）

- **不改 `InstanceDiagnosis` 模型**：v1 已定型，本方案纯粹做透传。
- **不改 LLM 可见的工具返回字符串**：与 v1 完全正交，避免回归风险。
- **不增加 DB 字段 / 索引**：先靠 JSONB 跑，索引按 EXPLAIN 分析后再加。
- **不阻塞**：`attach_tool_meta` 必须是同步 set，不发起 I/O。
- **meta 单条上限 4 KB**：`OmniObserver.log_tool_result` 写入前做 size 检查，
  超限丢弃 `meta` 并记 `log.warning`，避免 JSONL 行膨胀。
- **两个 adapter 必须同步改**：obs/CLAUDE.md 字段契约——任何字段名变更，
  `omnibot_jsonl.py` 与 `mhxy_jsonl.py` 都要对齐。
- **回调清空时机**：`on_tool_start` 必须 `set(None)`，否则上一次工具的
  meta 会泄漏给下一次同 trace 内的工具调用。

---

## 单元测试（必做）

新增 `tests/test_obs_tool_meta.py` —— 直接走 `tool.ainvoke()` 真实路径，
不 mock LangChain：

```python
import pytest
from unittest.mock import MagicMock
from langchain_core.tools import tool

from core.observability import (
    OmnibotObsCallbackHandler,
    OmniObserver,
    attach_tool_meta,
)


def make_handler():
    obs = MagicMock(spec=OmniObserver)
    h = OmnibotObsCallbackHandler(obs, trace_id="t1", provider="test")
    return obs, h


@tool
def _t_attach(payload: str) -> str:
    """probe."""
    attach_tool_meta({"kind": "x", "payload": payload})
    return "ok"


@tool
def _t_skip(payload: str) -> str:
    """probe."""
    return "ok"


@pytest.mark.asyncio
async def test_attach_to_tool_end():
    obs, h = make_handler()
    await _t_attach.ainvoke({"payload": "v"}, config={"callbacks": [h]})
    obs.log_tool_result.assert_called_once()
    kwargs = obs.log_tool_result.call_args.kwargs
    assert kwargs["meta"] == {"kind": "x", "payload": "v"}


@pytest.mark.asyncio
async def test_no_attach_yields_none():
    obs, h = make_handler()
    await _t_skip.ainvoke({"payload": "v"}, config={"callbacks": [h]})
    assert obs.log_tool_result.call_args.kwargs["meta"] is None


@pytest.mark.asyncio
async def test_sequential_no_leak():
    obs, h = make_handler()
    await _t_attach.ainvoke({"payload": "1"}, config={"callbacks": [h]})
    await _t_skip.ainvoke({"payload": "2"}, config={"callbacks": [h]})
    metas = [c.kwargs["meta"] for c in obs.log_tool_result.call_args_list]
    assert metas[0] == {"kind": "x", "payload": "1"}
    assert metas[1] is None        # on_tool_start 必须清空


@pytest.mark.asyncio
async def test_size_limit_drops():
    obs, h = make_handler()
    big = {"x": "y" * 5000}
    @tool
    def _t_big(payload: str) -> str:
        """."""
        attach_tool_meta(big)
        return "ok"
    await _t_big.ainvoke({"payload": "."}, config={"callbacks": [h]})
    assert obs.log_tool_result.call_args.kwargs["meta"] is None


@pytest.mark.asyncio
async def test_error_path_carries_meta():
    obs, h = make_handler()
    @tool
    def _t_err(payload: str) -> str:
        """."""
        attach_tool_meta({"kind": "x"})
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await _t_err.ainvoke({"payload": "."}, config={"callbacks": [h]})
    # on_tool_error 路径也应写入 meta
    assert obs.log_tool_result.call_args.kwargs["meta"] == {"kind": "x"}
```

新增 `tests/test_obs_diagnosis_e2e.py`（集成）：

mock `diagnose_instance` 返回 `InstanceIssue.ADB_OFFLINE` 的诊断，
调用 `check_instance_health.ainvoke(...)`，读取生成的 JSONL，
断言最后一条 `tool_result` 含 `meta.kind=="instance_diagnosis"` 与
`meta.code=="adb_offline"`。

---

## 验证方式

1. `pytest tests/test_obs_tool_meta.py tests/test_obs_diagnosis_e2e.py -v`
2. 重启 mhxy bot：`docker compose restart mhxy-tg-bot`
3. 在 Telegram 触发 `check_instance_health`
4. 触发 obs 摄取：`curl -X POST http://localhost:8000/api/ingest/mhxy?force=true`
5. 直查 DB 验证字段落地：
   ```sql
   docker exec obs-db-1 psql -U agent_obs -d agent_obs -c "
   SELECT timestamp, payload_json->'meta' AS meta
   FROM events
   WHERE event_type='tool_result'
     AND payload_json->'meta'->>'kind'='instance_diagnosis'
   ORDER BY timestamp DESC LIMIT 5;"
   ```
6. 浏览器打开 `http://localhost:3100/sessions/<session_id>`，
   找到刚才的 `check_instance_health` tool_result，应能看到结构化
   chip（端口/code/state/needs_human）与 steps 有序列表。
7. P1 验证完成后，再可选验证 `/stats/diagnoses` 与 `/stats/diagnoses/recent`
   返回结构化数据（curl + jq 即可）。

---

## 风险与回退

| 风险 | 处理 |
|------|------|
| `var_child_runnable_config` 在未来 LangChain 版本被改名/移除 | 单测 `test_attach_to_tool_end` 会立刻失败；fallback 路线是把现有 `@tool` 函数改 subclass `BaseTool` 拿 `run_manager` |
| 启用 parallel tool calling 后 single-slot 被覆盖 | 当前 sequential 假设；启用前需把 `_pending_meta` 改为 `dict[run_id, meta]`，并在 on_tool_start/end 用 `kwargs["run_id"]` 索引 |
| meta 体积膨胀拖慢 JSONL 写入 | 4 KB 上限 + filelock 仍是 append-only，影响有限 |
| 两个 adapter 漏改导致 mhxy 不出数据 | E2E 测试直跑覆盖 mhxy 路径 |
| 工具内部异常导致 meta 半成品落 | `attach_tool_meta` 内部 try/except 序列化错误时 silently 丢弃；on_tool_error 路径仍透传已挂载的 meta |

回退：单条 revert `core/observability.py` 即可——adapter 只是多读一个
字段，旧数据无 `meta` 直接走 None 分支，不会因回退坏库。

---

## Spike 验证记录（已删除文件，结论留档）

落地前已在 LangChain 0.3.84 / langchain-core 0.3.84（容器
`v2-omnistock-tg-bot`）下跑过 5 轮 spike，结论汇总：

| 验证目标 | 结果 |
|---------|------|
| 自建 `contextvars.ContextVar` 跨 callback 边界 | ❌ 双向隔离（`copy_context().run()` 包装） |
| `RunnableConfig` 里能否拿到 `run_id` | ❌ 永远 `None`，`callbacks` 是裸 list |
| `@tool` 函数声明 `run_manager` 参数 | ❌ 0.3.x 不注入，反被解析进 args schema |
| subclass `BaseTool` 拿 `run_manager` | ✅ 可用，作 fallback 备案 |
| `var_child_runnable_config` 反查 handler | ✅ **推荐方案**，sequential 场景全通过 |

如果未来 LangChain 升级出现兼容性问题，可参考上述结论快速重写一个
最小复现脚本（约 50 行）即可定位回归点。

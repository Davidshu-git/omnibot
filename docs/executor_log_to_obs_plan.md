# 方案：Windows Executor 执行日志接入 obs Timeline

## 目标

把 Windows 侧 `mhxy_bot/executor` 的请求级执行记录（每次 ADB / OCR / tap / wait 调用、耗时、成败、内部 timing）实时回流到 obs，与 NAS 端现有 mhxy session 的 timeline 对齐展示，**让大模型的一次工具调用 → 在 obs 里能直接看到 Windows 侧产生的全部底层动作**。

非目标：重新造可观测性（不上 OpenTelemetry）、不改既有 mhxy JSONL 契约、不让 executor 直连 NAS（保持单向 NAS→Windows 调用关系）。

---

## 现状盘点（编码前必读）

| 组件 | 现状 | 与本方案的关系 |
|------|------|----------------|
| `mhxy_bot/executor/main.py` | FastAPI，监听 8765，已有 `_StatsMiddleware` 生成/接受 `X-Request-ID` 并写入 `executor.log`（文本格式，RotatingFileHandler 10MB×3） | **新增 JSONL 写入器**，与文本日志并存 |
| `mhxy_bot/tools/executor_client.py` | NAS 侧 `httpx` 客户端，每次调用没透传任何上下文 header | **新增 trace/session header 透传**入口 |
| `mhxy_bot/executor/watchdog.py`（NAS 侧） | 已用 `MHXY_REMOTE_SSH_KEY=/root/.ssh_runtime/id_towin` 通过 SSH 重启 windows executor，已写 `executor_watchdog.jsonl` | **复用同一密钥**做 `scp` 增量拉日志 |
| `obs/api/app/adapters/mhxy_jsonl.py` | 已有 `_RUNNER_EVENT_TYPES` 白名单识别 task_event；session_id 取首行 `type=session` | **新建独立 adapter**，避免污染会话语义 |
| `obs/api/app/api/router.py` | 已有 `run_jsonl_ingest()` 通用幂等摄取；`/api/ingest/{project}` 端点 | **加一个 wrapper + 端点** |
| `obs/web/src/pages/sessions.tsx` | 已渲染 `task_event` 多 subtype（含 `executor_perf`），有 `TASK_EVENT_ICONS` 表 | **扩展 subtype 渲染分支** |

---

## 总体架构

```
┌─ Windows (192.168.100.149) ────────────────────────────────┐
│  mhxy_executor.exe                                         │
│   ├── executor.log                  (人类可读，保留)        │
│   └── executor_events_YYYYMMDD.jsonl  ← 新增结构化事件流   │
└─────────────▲───────────────────────────────┬──────────────┘
              │ NAS runner 调用时透传          │ scp 增量拉
              │ X-Trace-Id / X-Session-Id     │ (id_towin)
              │                               ▼
┌─ NAS (omnibot 容器) ───────────────────────────────────────┐
│  mhxy_bot/runner/* ─→ ExecutorClient                       │
│                                                            │
│  obs-api 容器                                              │
│   ├── executor_log_sync.py  (新增 watcher / cron)          │
│   │     scp sdw@win:executor_events_*.jsonl                │
│   │       → /logs/omnibot/mhxy/executor/                   │
│   ├── adapters/mhxy_executor_jsonl.py  (新增 adapter)      │
│   ├── /api/ingest/mhxy-executor       (新增端点)           │
│   └── 现有 ingestion service (复用)                        │
│                                                            │
│  PostgreSQL events 表                                      │
│   └── 事件 trace_id 与 NAS runner trace_id 一致            │
│                                                            │
│  obs-web                                                   │
│   └── sessions.tsx 渲染 task_event subtype:                │
│        executor_request / executor_internal                │
└────────────────────────────────────────────────────────────┘
```

**核心关联机制**：NAS runner 调 executor 时把 `trace_id` 和 `session_id` 放进 HTTP header，executor 写 JSONL 时把这两个值落到每行记录的顶层字段；obs adapter 直接用这两个字段挂回现有会话，**不为 executor 单独建会话**（无 NAS context 的 self-health 请求才挂到独立 session，见下文）。

---

## 数据契约

### JSONL schema（executor 侧产出）

文件路径：`<executor_dir>/executor_events_YYYYMMDD.jsonl`，UTC 日期切分，永不旋转覆盖（手动归档），每行一个 JSON。

字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | str | 必填 | `executor_request` / `executor_internal` / `executor_startup` |
| `timestamp` | str (ISO8601 UTC) | 必填 | 事件发生时刻 |
| `request_id` | str | 必填 | 中间件生成或来自 `X-Request-ID` |
| `session_id` | str \| null | 可选 | 来自 `X-Session-Id` header（NAS 端 mhxy session_id） |
| `trace_id` | str \| null | 可选 | 来自 `X-Trace-Id` header（NAS runner trace_id） |
| `host` | str | 必填 | windows 主机名（`socket.gethostname()`），用于 self-health session 拼装 |
| `port` | str \| null | 可选 | MuMu 端口（请求体里有则提取） |
| `method` | str | `executor_request` 必填 | HTTP 方法 |
| `path` | str | `executor_request` 必填 | 路径，例如 `/sense` |
| `status_code` | int | `executor_request` 必填 | HTTP 状态码 |
| `duration_ms` | float | `executor_request` 必填 | 端到端耗时 |
| `slow` | bool | `executor_request` 必填 | 是否超过 `_SLOW_REQ_THRESHOLD` |
| `op` | str | `executor_internal` 必填 | 内部操作名：`adb`、`ocr`、`screenshot` |
| `detail` | dict | 可选 | 操作子参数（命令、超时、缓存命中等），不放大段文本 |
| `success` | bool | `executor_internal` 必填 | 是否成功 |
| `error` | str \| null | 可选 | 错误摘要（截断 200 字符） |

**严禁字段**：截图二进制、OCR 全文（按需保留 `text_count` 即可，全文进 timeline 既费 token 又泄漏）。

### header 协议（NAS → Windows）

| Header | 值 |
|--------|-----|
| `X-Request-ID` | 复用现有，缺失则中间件生成 12hex |
| `X-Trace-Id` | NAS runner 当前 trace_id（task_run_id 或 `tg_session_xxx:tN`） |
| `X-Session-Id` | NAS 端 mhxy session_id（`tg_session_game-bot_<uid>_<date>`） |

executor 不解析 header 内容，原样落库。

---

## 改动清单（按文件）

### 1. `mhxy_bot/executor/main.py`（Windows 侧）

#### 1.1 新增 JSONL 事件写入器

在文件顶部、文本 logger 初始化后，加入独立的 `events_log` 写入器：

```python
import socket
import json as _json
from datetime import datetime, timezone

EVENTS_DIR = Path(os.getenv("EXECUTOR_EVENTS_DIR", str(LOG_DIR)))
EVENTS_DIR.mkdir(parents=True, exist_ok=True)
HOSTNAME = socket.gethostname()
_events_lock = threading.Lock()

def _events_path() -> Path:
    return EVENTS_DIR / f"executor_events_{datetime.now(timezone.utc):%Y%m%d}.jsonl"

def emit_event(event: dict) -> None:
    """线程安全地追加一行 JSON 到当日 events 文件。失败仅写入文本 log，不抛。"""
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    event.setdefault("host", HOSTNAME)
    line = _json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    try:
        with _events_lock:
            with _events_path().open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:
        log.warning("emit_event failed: %s", exc)
```

不使用 logging 模块——避免和 RotatingFileHandler 互相影响。

#### 1.2 启动事件

在 `_startup()` 末尾加：

```python
emit_event({
    "type": "executor_startup",
    "request_id": uuid.uuid4().hex[:12],
    "log_file": LOG_FILE,
    "events_dir": str(EVENTS_DIR),
    "adb_path": ADB_PATH,
})
```

#### 1.3 改造 `_StatsMiddleware`

在 `dispatch` 末尾（既有 `req_stats` 之前）插入：

```python
trace_id = request.headers.get("x-trace-id")
session_id = request.headers.get("x-session-id")
port = None
# 不解析 body（middleware 已读完），通过 starlette 传出来即可：见 1.4
port = getattr(request.state, "port", None)

emit_event({
    "type": "executor_request",
    "request_id": request_id,
    "session_id": session_id,
    "trace_id": trace_id,
    "method": request.method,
    "path": request.url.path,
    "status_code": status_code,
    "duration_ms": round(elapsed * 1000, 2),
    "slow": elapsed >= _SLOW_REQ_THRESHOLD,
    "port": port,
})
```

`/health` 也写——量很小，自检请求需要进 self-health session。

#### 1.4 把 `port` 传出来

middleware 拿不到 body。最稳的做法：在每个路由函数里写一行 `request.state.port = req.port` —— FastAPI 的 `Request` 可作为依赖注入：

```python
from fastapi import Request as FastAPIRequest

@app.post("/sense")
def sense(req: PortReq, request: FastAPIRequest):
    request.state.port = req.port
    ...
```

所有带 `port` 的端点都加，路由函数体不动。`/health` 不加（无 port）。

#### 1.5 内部事件

在 `_adb()` / `_ocr_items()` / `_screenshot_png()` 里发 `executor_internal`：

```python
# _adb 末尾
emit_event({
    "type": "executor_internal",
    "request_id": (request.headers.get("x-request-id") if 'request' in dir() else None) or "unknown",
    "op": "adb",
    "port": port,
    "detail": {"args": list(args)[:4], "rc": r.returncode, "timeout": timeout},
    "success": r.returncode == 0,
    "error": (r.stderr.decode(errors="replace").strip()[:200] if r.returncode != 0 else None),
})
```

⚠️ `_adb` 当前没有 `request` 上下文。**改造方式**：给 `_adb` / `_ocr_items` 增加可选 `request_id: str | None = None` 参数，由路由函数从 `request.state.request_id` 传入；middleware 同步把 `request_id` 写到 `request.state.request_id`。

如果不想改太多签名，**最低保留**：仅在 middleware 出口的 `executor_request` 事件里带 `port`，跳过内部细分。`executor_internal` 列为 v2 增量。

> **优先级**：v1 必须有 `executor_request` 和 `executor_startup`；`executor_internal` 是 v2，留口子但允许后做。本方案落地以 v1 为准。

#### 1.6 配置项（Windows 侧 `.env`）

```env
EXECUTOR_EVENTS_DIR=C:\Users\sdw\mhxy_executor\events
EXECUTOR_SLOW_REQ_THRESHOLD_SEC=8
```

`EXECUTOR_EVENTS_DIR` 不存在时回退到 `LOG_DIR`。

---

### 2. `mhxy_bot/tools/executor_client.py`（NAS 侧）

`ExecutorClient` 增加上下文 header 透传。

```python
class ExecutorClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = 30,
        *,
        session_id: str | None = None,
        trace_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session_id = session_id
        self._trace_id_provider = trace_id_provider

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._session_id:
            h["X-Session-Id"] = self._session_id
        if self._trace_id_provider:
            tid = self._trace_id_provider()
            if tid:
                h["X-Trace-Id"] = tid
        return h

    def _post(self, path: str, *, _timeout: int | None = None, **body) -> dict:
        r = httpx.post(
            f"{self._base}{path}",
            json=body,
            timeout=_timeout if _timeout is not None else self._timeout,
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    def health(self) -> bool:
        try:
            r = httpx.get(f"{self._base}/health", timeout=5, headers=self._headers())
            return r.status_code == 200
        except Exception:
            return False
```

#### 注入点

`mhxy_bot/runner/context.py` 创建 `ExecutorClient` 时传入：

```python
client = ExecutorClient(
    base_url=executor_url,
    session_id=runner_ctx.session_id,         # 已有字段；若没有需补
    trace_id_provider=lambda: runner_ctx.current_trace_id,  # 在 task_started 时 set
)
```

`current_trace_id` 在每次 `task_started` 时设置为 `task_run_id`，task 结束清空。**这一改造不能破坏 watchdog**——watchdog 单独构造 `ExecutorClient(base_url)`，不传 session/trace，发出去的请求 `session_id`/`trace_id` 自然为 `null`，归到 self-health session（见下）。

---

### 3. `mhxy_bot/executor/watchdog.py`（NAS 侧）

watchdog 已经有 `id_towin` 密钥到 windows 的 SSH 通路。在每轮 tick 末尾追加一次增量 scp。

#### 3.1 增量同步函数

```python
EXECUTOR_EVENTS_REMOTE_DIR = os.getenv(
    "MHXY_EXECUTOR_EVENTS_REMOTE_DIR",
    r"C:/Users/sdw/mhxy_executor/events",
)
EXECUTOR_EVENTS_LOCAL_DIR = Path(os.getenv(
    "MHXY_EXECUTOR_EVENTS_LOCAL_DIR",
    "/logs/omnibot/mhxy/executor",
))

def sync_executor_events() -> dict:
    """从 windows 拉取 executor_events_*.jsonl 增量到本地。
    使用 scp -p 保留 mtime；rsync 在 windows 端不一定可用，故走 scp + 列表比对。"""
    EXECUTOR_EVENTS_LOCAL_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 列出远端文件 + size + mtime
    list_cmd = [
        "ssh", "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        SSH_TARGET,
        "powershell -Command \"Get-ChildItem '{}\\executor_events_*.jsonl' | "
        "Select-Object Name,Length,@{{n='Mtime';e={{$_.LastWriteTimeUtc.ToString('o')}}}} | ConvertTo-Json -Compress\"".format(
            EXECUTOR_EVENTS_REMOTE_DIR.replace("/", "\\")
        ),
    ]
    # 解析 JSON、与本地 size+mtime 比对，差异文件加入 to_pull
    # 仅同步当日和昨日两个文件即可（日切边界），早期文件视为冷数据

    # 2. scp 拉变化的文件（覆盖整文件，executor 是 append-only，scp 后 size 单调增）
    pulled: list[str] = []
    for fname in to_pull:
        local = EXECUTOR_EVENTS_LOCAL_DIR / fname
        scp_cmd = [
            "scp", "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            f"{SSH_TARGET}:{EXECUTOR_EVENTS_REMOTE_DIR}/{fname}",
            str(local),
        ]
        r = subprocess.run(scp_cmd, capture_output=True, timeout=60)
        if r.returncode == 0:
            pulled.append(fname)

    return {"pulled": pulled, "count": len(pulled)}
```

#### 3.2 集成到主循环

watchdog 每 tick（默认 60s）跑一次健康检查，**改造**：每 N 轮（默认 1 即每轮）顺带跑 `sync_executor_events()`，拉到的文件落 `EXECUTOR_EVENTS_LOCAL_DIR`，再调用 `notify_obs_ingest()`：

```python
def notify_obs_ingest() -> None:
    try:
        requests.post("http://obs-api:8000/api/ingest/mhxy-executor", timeout=10)
    except Exception as exc:
        log.warning("notify obs ingest failed: %s", exc)
```

> 也可以让 obs-api 自己挂 watcher（监听文件变化）做 ingest。watchdog 直接 POST 更简单、链路短。

#### 3.3 失败恢复

- scp 单次失败不影响 watchdog 主流程，记录 `executor_watchdog.jsonl`
- 连续 5 次同步失败时，TG 告警一次（`tg_send`）

---

### 4. obs-api 改造

#### 4.1 新建 `obs/api/app/adapters/mhxy_executor_jsonl.py`

骨架（编码时参照 `mhxy_jsonl.py` 写）：

```python
from __future__ import annotations
import glob as glob_module
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.adapters.common import now as _now, parse_ts as _parse_ts
from app.schemas.events import (
    EventType, NormalizedEvent, RawEventBlob, SessionRef, SourceRef,
)

PROJECT_ID = "mhxy"
AGENT_ID = "windows-executor"
SOURCE = "mhxy_executor_jsonl"

class MhxyExecutorJsonlAdapter:
    source_type = SOURCE

    def __init__(self, log_dir: str):
        self.log_dir = log_dir

    def discover_sources(self) -> list[SourceRef]:
        files = glob_module.glob(os.path.join(self.log_dir, "executor_events_*.jsonl"))
        return [SourceRef(source_id=Path(f).stem, path=f) for f in sorted(files)]

    def scan_sessions(self, source: SourceRef) -> list[SessionRef]:
        # 一个文件可能横跨多个 NAS session（按 session_id 分组）
        # 真实做法：load_events 阶段动态决定 session_id，scan 时返回单个虚拟 ref
        return [SessionRef(session_id="<dynamic>", source_ref=source)]

    def load_events(self, session: SessionRef):
        """每行根据 session_id / host 决定归属 session：
        - 有 session_id → 直接挂到该 NAS session（必须已存在；不存在则归 unknown 桶）
        - 无 session_id（自检 / watchdog 调用） → 归 'executor_self_<host>_<UTC日期>'
        """
        path = session.source_ref.path
        raw_blobs: list[RawEventBlob] = []
        events: list[NormalizedEvent] = []

        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                sess = rec.get("session_id")
                if not sess:
                    host = rec.get("host") or "unknown"
                    ts = _parse_ts(rec.get("timestamp"))
                    date = (ts or datetime.now(timezone.utc)).strftime("%Y%m%d")
                    sess = f"executor_self_{host}_{date}"

                external_key = self._content_key(sess, rec, lineno)

                blob = RawEventBlob(
                    project_id=PROJECT_ID, source=SOURCE,
                    external_key=external_key, collected_at=_now(),
                    payload_json=rec, payload_hash="",
                )
                raw_blobs.append(blob)

                ev = self._map(rec, sess, external_key)
                if ev is not None:
                    events.append(ev)

        return raw_blobs, events

    def _content_key(self, session_id: str, rec: dict, lineno: int) -> str:
        digest = hashlib.sha256(
            json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:20]
        return f"mhxy_executor:{session_id}:{digest}"

    def _map(self, rec: dict, session_id: str, external_key: str) -> NormalizedEvent | None:
        rtype = rec.get("type")
        ts = _parse_ts(rec.get("timestamp"))
        trace_id = rec.get("trace_id")

        # 把所有 executor 类型映射成 task_event，subtype 落在 payload.type
        # 与现有前端渲染契约对齐
        if rtype not in {"executor_request", "executor_internal", "executor_startup"}:
            return None

        return NormalizedEvent(
            event_id=external_key,
            project_id=PROJECT_ID,
            agent_id=AGENT_ID,
            session_id=session_id,
            trace_id=trace_id,
            run_id=rec.get("request_id"),  # request_id 当作子运行 id
            timestamp=ts,
            source=SOURCE,
            event_type=EventType.TASK_EVENT,
            payload=rec,
        )
```

**关键设计点**：
- `event_type` 统一用 `task_event`，`payload.type` 携带子类型 → 复用前端 `task_event` 分支，免改 schema 枚举。
- session_id 动态决定：携带 NAS session 的事件挂到现有 mhxy session（前端会和 message / model_call / tool_call 同时显示）；自检事件挂到 `executor_self_*` 独立 session。
- 同一行的 `external_key` 用 `sha256(canonical_json)` —— append-only 文件场景下幂等。

#### 4.2 `obs/api/app/api/router.py` 增加 wrapper + 端点

```python
async def run_mhxy_executor_ingest(force: bool = False) -> dict:
    from app.adapters.mhxy_executor_jsonl import MhxyExecutorJsonlAdapter
    log_dir = os.getenv("MHXY_EXECUTOR_LOG_DIR", "/logs/omnibot/mhxy/executor")
    return await run_jsonl_ingest(
        project_id="mhxy",
        source_type="mhxy_executor_jsonl",
        agent_id="windows-executor",
        force=force,
        adapter=MhxyExecutorJsonlAdapter(log_dir=log_dir),
    )

@router.post("/ingest/mhxy-executor")
async def ingest_mhxy_executor(force: bool = Query(False)):
    result = await run_mhxy_executor_ingest(force=force)
    return result
```

并入 `run_jsonl_ingest` 既得：去重、cursor、SSE 广播（无需改）。

#### 4.3 docker-compose.yml + .env

`obs/docker-compose.yml` 给 `api` 服务挂载 NAS 上的 executor 日志目录：

```yaml
services:
  api:
    volumes:
      - ${OMNIBOT_HOME}/data/mhxy/observability/sessions:/logs/mhxy/sessions:ro
      - ${OMNIBOT_HOME}/data/mhxy/observability/executor:/logs/omnibot/mhxy/executor:ro
    environment:
      MHXY_EXECUTOR_LOG_DIR: /logs/omnibot/mhxy/executor
```

watchdog 容器同步写到 host 路径 `${OMNIBOT_HOME}/data/mhxy/observability/executor/`，**watchdog 容器和 obs-api 容器需要 bind mount 同一个 host 目录**（一个 rw 一个 ro）。

`.env.example` 新增：

```env
MHXY_EXECUTOR_LOG_DIR=/logs/omnibot/mhxy/executor
MHXY_EXECUTOR_EVENTS_REMOTE_DIR=C:/Users/sdw/mhxy_executor/events
MHXY_EXECUTOR_EVENTS_LOCAL_DIR=/logs/omnibot/mhxy/executor
```

#### 4.4 前端 `obs/web/src/pages/sessions.tsx`

`TASK_EVENT_ICONS` 加：

```ts
executor_request:  "🪟",
executor_internal: "🔧",
executor_startup:  "🚀",
```

在 `t === "task_event"` 分支里增加 subtype 渲染：

```tsx
{subtype === "executor_request" && <>
  <span style={{ color: "var(--orange)", fontFamily: "var(--font-mono)" }}>
    {p.method as string} {p.path as string}
  </span>
  <span style={{ color: (p.status_code as number) < 400 ? "var(--green)" : "var(--red)", fontFamily: "var(--font-mono)" }}>
    {p.status_code as number}
  </span>
  <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
    {fmtMsValue(p.duration_ms)}
  </span>
  {p.slow && <span style={chipStyle("warn")}>慢请求</span>}
  {p.port && <span style={{ color: "var(--teal)", fontFamily: "var(--font-mono)" }}>:{p.port as string}</span>}
</>}

{subtype === "executor_startup" && <>
  <span style={{ color: "var(--green)" }}>executor 启动</span>
  <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{p.host as string}</span>
</>}
```

`executor_internal` 暂用通用 fallback（`hasExpandedDetails` 走默认渲染足够）。

---

## 时序示意（一次大模型 tool 调用）

```
LLM ──tool_call──→ NAS runner.actions.tap_text
                     │
                     │  TraceContext{trace_id=task_run_xxx,session_id=tg_session_yyy}
                     ▼
              ExecutorClient.tap_text
                     │
                     │  POST /tap_text
                     │  Headers:
                     │   X-Trace-Id: task_run_xxx
                     │   X-Session-Id: tg_session_yyy
                     │   X-Request-ID: 9f2c0a1...
                     ▼
              Windows executor
                     │
                     ├─ _ocr_items()  (内部 timing)
                     ├─ _adb tap
                     │
                     ▼
              executor_events_20260508.jsonl (append):
                {"type":"executor_request","trace_id":"task_run_xxx",
                 "session_id":"tg_session_yyy","method":"POST",
                 "path":"/tap_text","status_code":200,"duration_ms":1832,...}
                     │
                     │  watchdog scp (≤60s)
                     ▼
              /logs/omnibot/mhxy/executor/executor_events_20260508.jsonl
                     │
                     │  POST /api/ingest/mhxy-executor (watchdog 触发)
                     ▼
              MhxyExecutorJsonlAdapter → events 表
                  session_id = tg_session_yyy
                  trace_id   = task_run_xxx
                  event_type = task_event
                  payload.type = executor_request
                     │
                     │  SSE broadcast
                     ▼
              obs-web sessions.tsx Timeline 渲染
              （与现有 tool_call、tool_result 在同一 trace 下相邻显示）
```

---

## 接入约束

- **executor 永不直连 NAS**：JSONL 落本地文件，让 NAS 主动拉。降低部署复杂度，与现有 watchdog SSH 通道一致。
- **JSONL 行长度上限 16 KB**：超出截断 detail 字段；不能让一行日志撑爆 ingestion。
- **session_id 在 NAS 端创建**：obs adapter 不创建 session，`upsert_session` 由 ingestion service 处理；NAS 端缺 session 的请求归 `executor_self_*`。
- **trace_id 必须从 NAS runner 透传**：禁止 executor 自造 trace_id，否则与 NAS 端的关联永远断掉。
- **scp 频率上限 1 次 / 60s**：避免 windows IO 抖动。日切边界仅同步当日和昨日两个文件。
- **不复用 mhxy_jsonl adapter**：避免 `_RUNNER_EVENT_TYPES` 白名单膨胀和 session_id 提取逻辑分叉。

---

## 验证步骤（编码完成后跑一遍）

### 1. Windows 端

```powershell
# 重启 executor，发一笔请求
curl http://localhost:8765/health
type C:\Users\sdw\mhxy_executor\events\executor_events_20260508.jsonl
# 应至少看到 1 行 executor_startup + 1 行 executor_request
```

### 2. NAS 端 watchdog

```bash
docker exec v2-omnimhxy-watchdog python -c "
from mhxy_bot.executor.watchdog import sync_executor_events
print(sync_executor_events())
"
ls -la /volume1/server/.openclaw/workspace/projects/omnibot/data/mhxy/observability/executor/
```

### 3. obs ingest

```bash
curl -X POST "http://localhost:8000/api/ingest/mhxy-executor?force=true"
# 期望 inserted > 0
```

### 4. 数据库

```bash
docker exec obs-db-1 psql -U agent_obs -d agent_obs -c \
  "select event_type, payload_json->>'type', count(*) from events
   where source='mhxy_executor_jsonl' group by 1,2 order by 3 desc;"
```

### 5. 前端

打开 `http://localhost:3100/sessions`，找到当日某条 mhxy session，timeline 中应能看到 `🪟 executor_request` 与 `tool_call` 在同一 trace 下相邻；左侧列表应另有一条 `executor_self_<host>_<date>` 会话承载自检请求。

---

## 风险与权衡

| 风险 | 应对 |
|------|------|
| Windows 时钟与 NAS 偏差 → timeline 顺序错乱 | executor 用 UTC 时间戳，NAS 同；偏差 > 5s 时在 watchdog 告警 |
| scp 串行拉取大文件慢 | 日切 + 仅拉两天文件；executor 单文件预计 < 50 MB / 天 |
| executor 写盘失败导致丢事件 | 失败仅记 `executor.log`，不阻塞响应。允许丢，不追求强一致 |
| NAS session 还没创建就先收到 executor 事件 | `ingest_batch` 已 upsert session（外键不强制），事件不会丢；mhxy ingest 后续补上 session 行 |
| 大模型对话频率变高 → 事件量爆涨 | 当前 executor QPS 约 5/s；按一行 ~300 字节估算，日产出 < 130 MB，PG 单表 InnoDB 吞吐无压力 |

---

## 不在本方案范围

- 把 `executor.log` 文本日志也吃进 obs（结构化代价过大）
- executor 端追加告警 / 自愈逻辑（已有 watchdog）
- 跨 windows 多节点的日志聚合（当前只有一台 windows）

---

## 编码顺序建议

1. **executor `emit_event` + `executor_request` 落盘**（独立可验证，先跑通 windows 端）
2. **NAS `ExecutorClient` header 透传 + runner 上下文注入**（不影响现有功能）
3. **watchdog scp 同步 + obs 文件挂载**（端到端拉通）
4. **adapter + ingest 端点**（数据进库）
5. **前端渲染分支**（最后做，schema 稳定后才动 UI）

每一步都能独立验证（写完即测），出问题不会回滚整条链路。

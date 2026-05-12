# 方案：instances.json 动态同步（基于 ADB 自动发现）

> v1.0

---

## 背景与问题

`instances.json` 是 mhxy_bot 唯一的实例配置来源，runner、tg_main、game_tools、watchdog 均从此文件读取端口列表。  
当前该文件**纯手工维护**，没有任何自动化同步机制。

已验证的现实问题（2026-05-12 实测）：
- instances.json 记录 10 个实例（端口 5557–5575 奇数序列）
- Windows ADB 实际在线只有 7 个（`adb devices` 返回 emulator-5556/5558/5560/5566/5570/5572/5574）
- 3 个僵尸条目（5563/5565/5569）持续存在，导致 obs 面板显示 `/10` 而非实际的 `/7`

使用场景：实例数变化频率约**一天数次**，手动编辑 + 重启 watchdog 代价过高。

---

## 端口约定（关键背景）

MuMu 模拟器使用以下约定（已在 `executor/main.py::_port_to_addr` 注释中明确）：

| 概念 | 格式 | 示例 |
|------|------|------|
| instances.json port | 奇数 | 5557 |
| ADB serial | `emulator-(port-1)`，即偶数 | `emulator-5556` |

转换公式：
- `adb devices` 输出 `emulator-N` → instances port = `N + 1`（N 为偶数）
- instances port P（奇数）→ ADB serial = `emulator-(P-1)`

`_port_to_addr()` 已实现正向转换（instances port → ADB serial），新增接口做反向解析即可。

---

## 改动范围（三处，相互独立）

### 1. executor/main.py — 新增 `/list_devices` 接口

**位置**：`mhxy_bot/executor/main.py`，在现有 `/health` 路由附近添加。

**逻辑**：
```python
@app.get("/list_devices")
def list_devices():
    """列出当前 ADB 可见的所有模拟器，返回 instances.json 格式的奇数端口列表。"""
    adb = _resolve_adb()   # 复用已有的 adb 路径解析
    r = subprocess.run([adb, "devices"], capture_output=True, timeout=10)
    ports = []
    for line in r.stdout.decode(errors="replace").splitlines():
        m = re.match(r"emulator-(\d+)\s+device", line)
        if m:
            even = int(m.group(1))
            ports.append(even + 1)   # 转成奇数，与 instances.json 对齐
    return {"ports": sorted(ports), "count": len(ports)}
```

**返回示例**：
```json
{"ports": [5557, 5559, 5561, 5567, 5571, 5573, 5575], "count": 7}
```

**注意**：
- adb 可执行路径在 Windows 上不在 PATH，直接复用 `executor/main.py` 已有的 `ADB_PATH` 模块级常量即可，无需额外解析。经实测，可信路径为 `C:\Program Files\Netease\MuMu\nx_main\adb.exe`（已作为 `ADB_PATH` 和 `WINDOWS_ADB_PATH` 的统一默认值）。
- 只返回状态为 `device` 的行，忽略 `offline` / `unauthorized`。

---

### 2. game_tools.py — 新增 `sync_instances` 工具

**位置**：`mhxy_bot/tools/game_tools.py`，在 `make_game_tools()` 工厂函数内，与 `batch_recognize_schools` 并列注册。

**工具描述**（供 LLM 调用决策）：
> 从 Windows executor 查询当前实际在线的模拟器列表，与 instances.json 比对，自动增删条目，保留已有门派/备注元数据。用于实例数量变化后同步配置。

**核心逻辑**：

```python
@tool
def sync_instances() -> str:
    """同步 instances.json 与 ADB 实际在线设备，自动增删实例条目。"""
    # 1. 从 executor 拿在线端口
    resp = executor._get("/list_devices", _timeout=15)
    live_ports = set(resp.get("ports", []))

    # 2. 读当前配置
    data = _load_instances()
    current = {inst["port"]: inst for inst in data.get("instances", [])}
    current_ports = set(current.keys())

    # 3. 计算 diff
    to_add = live_ports - current_ports
    to_remove = current_ports - live_ports

    # 4. 执行变更
    #    新增：追加空白条目
    for p in sorted(to_add):
        data.setdefault("instances", []).append({"port": p})
    #    移除：从 instances 列表删除
    data["instances"] = [inst for inst in data["instances"]
                         if inst["port"] not in to_remove]
    #    清理 groups：移除已不存在端口的 leader/member 引用
    _cleanup_groups(data, to_remove)

    # 5. 写回
    data["sync_time"] = datetime.now().isoformat()
    INSTANCES_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    # 6. 返回摘要
    lines = [f"同步完成（在线 {len(live_ports)} 个）："]
    if to_add:
        lines.append(f"  ✅ 新增：{sorted(to_add)}")
    if to_remove:
        lines.append(f"  🗑 移除：{sorted(to_remove)}")
    if not to_add and not to_remove:
        lines.append("  无变更，配置已是最新")
    return "\n".join(lines)
```

**`_cleanup_groups` 辅助函数**：
- 遍历 `data["groups"]`，移除 `leader.port` 或 `members[].port` 在 `to_remove` 中的条目
- leader 被移除 → 整组删除；member 被移除 → 从该组 members 列表中摘除

**tg_main.py 状态文案**（`TOOL_STATUS_MAP` 追加）：
```python
"sync_instances": "🔄 正在同步实例列表，请稍候...",
```

---

### 3. watchdog.py — ports 热重载

**位置**：`mhxy_bot/executor/watchdog.py`，`run_once()` 函数开头。

**改动**（单行）：

```python
# 改前：ports 在 main() 启动时固定，run_once 接收参数
def run_once(iteration, consecutive_failures, ports):
    ...

# 改后：run_once 每轮自己重读
def run_once(iteration, consecutive_failures, ports):
    ports = load_ports()   # 每轮重新读取，热更新
    ...
```

同时 `main()` 里的 `ports = load_ports()` 保留（用于启动日志打印），传入 `run_once` 的参数仍然保留签名兼容性。

**效果**：`sync_instances` 写完 instances.json 后，watchdog 下一个 check 周期（默认 60s）自动拿到新端口列表，obs 面板无需重启即可更新。

---

## 数据流（改动后）

```
用户 Telegram："同步一下实例"
    ↓
LLM 调用 sync_instances()
    ↓
ExecutorClient.GET /list_devices
    ↓ (Windows executor)
adb devices → 解析 emulator-N → port = N+1
    ↓
返回 live_ports = [5557, 5559, 5561, 5567, 5571, 5573, 5575]
    ↓
比对 instances.json（10个）→ to_remove={5563,5565,5569}, to_add={}
    ↓
写回 instances.json（7个）+ 清理 groups
    ↓
watchdog 下一轮（≤60s）load_ports() 重读 → app_health 只查 7 个端口
    ↓
obs 面板更新为 X/7
```

---

## 文件改动清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `mhxy_bot/executor/main.py` | 新增接口 | `GET /list_devices`，解析 adb devices 返回奇数端口 |
| `mhxy_bot/tools/executor_client.py` | 可能新增方法 | `list_devices()` 封装 GET 调用（视现有 `_get` 是否通用） |
| `mhxy_bot/tools/game_tools.py` | 新增工具 | `sync_instances` + `_cleanup_groups` 辅助函数 |
| `mhxy_bot/tg_main.py` | 小改 | `TOOL_STATUS_MAP` 追加 `sync_instances` 状态文案 |
| `mhxy_bot/executor/watchdog.py` | 单行改动 | `run_once()` 开头加 `ports = load_ports()` |

---

## 实现注意事项

1. **adb 路径**：Windows executor 的 adb.exe 不在系统 PATH，实测路径为 `C:\Program Files\Netease\MuMu\nx_main\adb.exe`。`/list_devices` 接口需复用 executor 现有的 adb 路径定位逻辑，不能硬编码。

2. **groups 清理策略**：leader 被移除时整组删除（组无头无意义）；member 被移除时仅摘除该成员，不解散整组（leader 仍在线则组仍有效）。

3. **school 保留**：`to_add` 的新端口只追加 `{"port": P}` 空白条目，不自动识别门派。如需填充 school，让 LLM 在 `sync_instances` 后顺手调 `batch_recognize_schools`，两步分离职责。

4. **并发安全**：instances.json 读写没有锁保护，`sync_instances` 和 `batch_recognize_schools` 理论上可能并发。实际上 LangChain Agent 是串行工具调用，Telegram 单用户场景无并发风险，不需要额外保护。

5. **executor_client `_get` 方法**：需确认 `ExecutorClient` 是否已有通用 GET 封装；若只有 `_post`，需补充 `_get` 或为 `list_devices` 单独写 `requests.get` 调用。

---

## 验证方法

```bash
# 1. 验证 /list_devices 接口
curl http://192.168.100.149:8765/list_devices

# 2. 验证 sync_instances 工具（在容器内跑）
docker exec v2-omnimhxy-tg-bot python -c "
from mhxy_bot.tools.game_tools import make_game_tools
from pathlib import Path
tools = make_game_tools(Path('/app/data/mhxy/agent_workspace'))
sync = next(t for t in tools if t.name == 'sync_instances')
print(sync.invoke({}))
"

# 3. 验证 watchdog 热重载：修改 instances.json 后等一个轮询周期，观察 obs 面板
curl http://localhost:8000/api/external/mhxy-executor/status | python -m json.tool | grep -A5 app_health
```

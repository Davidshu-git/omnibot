# 方案：把"重连实例"作为独立工具暴露给 LLM

## 背景与现状

mhxy_bot 当前 LLM 可见的工具集（`mhxy_bot/tools/game_tools.py::make_game_tools`）里
**没有独立的"重连"工具**。LLM 触发重连的唯一路径是 `check_instance_health` /
`batch_check_all_instances` —— 它们在内部走 `diagnose_instance`，且只在屏幕状态
被识别为 `DISCONNECTED` 或 `LOGIN_SCREEN` 时才隐式调用 `try_reconnect`。

而 Telegram `/reconnect` 命令背后的 `_reconnect_sync`（`mhxy_bot/tg_main.py`）把
`{DISCONNECTED, LOGIN_SCREEN, ANDROID_HOME}` 都视为可重连状态，并在所有
`reconnect_actions = ["重新登录", "确定"]`、"梦幻西游" 桌面图标、"登录游戏"、
更新弹窗、过场动画等场景下推进流程。

由此产生两个问题：

1. **能力错位**：`/reconnect` 能处理 ANDROID_HOME 等状态，LLM 走诊断却拿到
   `needs_human=True` 的最终结论，无法发起恢复。
2. **意图错位**：用户在 Telegram 用自然语言说"把 5557 重连一下" / "把所有掉线
   实例拉起来"，LLM 只能选择 `check_instance_health`（语义偏向"体检"）。这条路径
   既不直观，也无法表达"我**就是**要执行恢复"的意图。

`/reconnect` 命令、"🔌 掉线重连" 内联按钮、`runner/recovery.py` 的步级自动恢复
都能直接调到 `try_reconnect`，唯独 LLM tool call 不能。

## 目标

把"重连实例"提升为 LLM 可主动调用的一类 tool，与 `check_instance_health` /
`batch_check_all_instances` 平级，覆盖单端口与批量两个变体。重连仍走
`mhxy_bot/runner/instance_recovery.py::try_reconnect`，不引入新的恢复路径。

## 设计要点

1. **新工具命名沿用现有约定**：`reconnect_instance(port)` 与
   `batch_reconnect_instances(ports="")`。
2. **复用 `_reconnect_sync` 的"按当前状态决定 skip / reconnect"语义**，但不依赖
   `TelegramBotBase` 实例方法，因此把核心循环下沉成 `instance_recovery` 模块级
   helper（见下文 §1）。`tg_main.py::_reconnect_sync` 改为调用该 helper，避免
   两份逻辑漂移。
3. **可重连状态集合保持唯一来源**：在 `instance_recovery` 内定义
   `RECONNECTABLE_STATES = {DISCONNECTED, LOGIN_SCREEN, ANDROID_HOME}`，
   `_reconnect_sync` 与新 tool 都从这里读取。
4. **超时**：默认 60s（与 `/reconnect` 一致）。Tool 不暴露 timeout 参数 —— 暴露
   只会鼓励 LLM 乱填，且更长的 timeout 会让 Agent 回合卡死。
5. **observability**：tool 调用本身已经被 LangChain callback 记录到 obs
   （tool_started/tool_completed），新工具仅再额外调 `attach_tool_meta` 上报
   `kind=reconnect` 的结构化结果，不重复 events.scan_started / reconnect_port
   （那条事件流是 Telegram 命令路径专用的）。
6. **不引入并发**：`batch_reconnect_instances` 顺序执行，与
   `batch_check_all_instances` 一致。原因：ADB 顺序读屏 + OCR 阻塞，并发反而
   触发 executor 限流。
7. **不和 `check_instance_health` 内的隐式重连去重**：两条路径目标不同 ——
   `check_instance_health` 是"先体检，必要时附带恢复"，`reconnect_instance`
   是"明确执行恢复"。LLM 的 system prompt 要明确这条边界（见下文 §3）。

## 改动范围（3 个文件）

### 1. `mhxy_bot/runner/instance_recovery.py`

新增模块级常量与 helper，抽出 `_reconnect_sync` 的核心循环。

```python
from mhxy_bot.runner.models import InstanceState

# 当前 state 处于这些值时，try_reconnect 才有意义
RECONNECTABLE_STATES: frozenset[InstanceState] = frozenset({
    InstanceState.DISCONNECTED,
    InstanceState.LOGIN_SCREEN,
    InstanceState.ANDROID_HOME,
})


def reconnect_one_port(
    ctx: "RunnerContext",
    *,
    timeout_sec: int = 60,
) -> dict:
    """对单端口执行"按当前状态决定 skip / reconnect"流程，返回结构化结果。

    Returns:
        dict 形如:
        - {"action": "skipped",     "state": "<initial_state>"}
        - {"action": "reconnected", "final_state": "<state>"}
        - {"action": "failed",      "final_state": "<state>"}
    """
    from mhxy_bot.runner.perception import detect_screen_state

    state = detect_screen_state(ctx)
    if state not in RECONNECTABLE_STATES:
        return {"action": "skipped", "state": state.value}

    ok = try_reconnect(ctx, timeout_sec=timeout_sec)
    final = detect_screen_state(ctx).value
    return {
        "action": "reconnected" if ok else "failed",
        "final_state": final,
        "initial_state": state.value,
    }
```

`try_reconnect` 自身的实现不动。

### 2. `mhxy_bot/tg_main.py::_reconnect_sync`

改为调用新 helper，保留事件埋点：

```python
def _reconnect_sync(self, ports, observer=None, trace_id=None):
    from mhxy_bot.runner.task_loader import build_context, make_executor
    from mhxy_bot.runner.instance_recovery import reconnect_one_port
    from mhxy_bot.runner import events

    executor = make_executor()
    if observer:
        scan_ctx = build_context("*", executor, observer=observer, trace_id=trace_id)
        events.scan_started(scan_ctx, "reconnect", ports)

    results = []
    for port in ports:
        ctx = build_context(port, executor, observer=observer, trace_id=trace_id)
        outcome = reconnect_one_port(ctx, timeout_sec=60)
        if outcome["action"] == "skipped":
            events.reconnect_port(ctx, outcome["state"], None, outcome["state"])
            results.append({"port": port, "action": "skipped",
                            "state": outcome["state"]})
        else:
            events.reconnect_port(
                ctx,
                outcome["initial_state"],
                outcome["action"] == "reconnected",
                outcome["final_state"],
            )
            results.append({
                "port": port,
                "action": outcome["action"],
                "final_state": outcome["final_state"],
            })
    return results
```

行为完全等价；目的是让 helper 成为唯一的重连入口。

### 3. `mhxy_bot/tools/game_tools.py`

在 `make_game_tools` 内新增两个 `@tool`，并加入返回列表。

```python
@tool
def reconnect_instance(port: str) -> str:
    """主动对指定模拟器实例执行重连恢复（处理掉线 / 登录界面 / 安卓桌面状态）。
    若实例当前不在可重连状态（如已在主界面），则跳过。最长等待 60 秒。

    Args:
        port: 模拟器端口号，如 "5557" 或 "127.0.0.1:5557"

    Returns:
        重连结果字符串：包含初始状态、动作（reconnected / failed / skipped）、最终状态。
    """
    try:
        from core.observability import attach_tool_meta
        from mhxy_bot.runner.context import RunnerContext
        from mhxy_bot.runner.instance_recovery import reconnect_one_port

        ctx = RunnerContext(executor=executor, port=_port_to_str(port))
        outcome = reconnect_one_port(ctx, timeout_sec=60)
        attach_tool_meta({
            "kind": "reconnect",
            "port": _port_to_str(port),
            **outcome,
        })

        action = outcome["action"]
        if action == "skipped":
            return (f"⏭️ 端口 {port} 跳过重连\n"
                    f"  当前状态：{outcome['state']}（无需恢复）")
        prefix = "✅" if action == "reconnected" else "❌"
        return (
            f"{prefix} 端口 {port} 重连{'成功' if action == 'reconnected' else '失败'}\n"
            f"  初始状态：{outcome['initial_state']}\n"
            f"  最终状态：{outcome['final_state']}"
        )
    except Exception as e:
        return f"❌ 重连异常：{type(e).__name__} - {e}"


@tool
def batch_reconnect_instances(ports: str = "") -> str:
    """批量对所有实例或指定实例（逗号分隔端口）执行重连。
    非可重连状态的实例会被跳过。每个实例最长等待 60 秒，实例数多时耗时较长。

    Args:
        ports: 逗号分隔的端口列表，为空时对所有实例执行重连。

    Returns:
        批量重连汇总：包含成功 / 失败 / 跳过统计与各实例结果。
    """
    try:
        if ports.strip():
            port_list = [p.strip() for p in ports.split(",") if p.strip()]
        else:
            port_list = [str(inst["port"]) for inst in _load_instances().get("instances", [])]
        if not port_list:
            return "❌ 没有可用实例"

        ok = bad = skip = 0
        lines = []
        for port in port_list:
            result = reconnect_instance.invoke({"port": port})
            first_line = result.split("\n")[0] if result else ""
            lines.append(first_line)
            if first_line.startswith("✅"):
                ok += 1
            elif first_line.startswith("❌"):
                bad += 1
            elif first_line.startswith("⏭️"):
                skip += 1

        return (f"共重连 {len(port_list)} 个实例"
                f"（{ok} 成功 / {bad} 失败 / {skip} 跳过）：\n"
                + "\n".join(lines))
    except Exception as e:
        return f"❌ 批量重连失败：{type(e).__name__} - {e}"
```

最后把这两个工具加进 `make_game_tools` 返回的列表，建议位置紧跟在
`batch_check_all_instances` 之后，与"诊断"族放一起：

```python
return [
    get_instances,
    batch_recognize_schools,
    check_instance_health,
    batch_check_all_instances,
    reconnect_instance,            # 新增
    batch_reconnect_instances,     # 新增
    capture_screenshot,
    ...
]
```

### 4. `mhxy_bot/tg_main.py::get_tool_status_map`

新增两个状态文案，让 Telegram"正在……"提示对得上：

```python
"reconnect_instance":         "🔌 正在重连模拟器实例...",
"batch_reconnect_instances":  "🔌 正在批量重连模拟器实例，请耐心等待...",
```

### 5. `mhxy_bot/agent.py::GAME_SYSTEM_PROMPT`

在"## 你的能力"小节里把诊断与恢复拆成两条，明确边界，避免 LLM 在用户说
"重连"时仍然只用 `check_instance_health`：

```
- 实例诊断：单端口体检（check_instance_health）、批量体检（batch_check_all_instances）
  —— 体检会在检测到掉线/登录界面时附带尝试恢复，用于"先看看状态"。
- 实例恢复：主动重连指定端口（reconnect_instance）、批量重连（batch_reconnect_instances，
  ports 留空=全部实例）—— 用于"用户明确要求恢复"或体检后已知需要恢复的场景。
```

并在"## 操作准则"里新增一条：

```
7. 用户说"重连/恢复/拉起"等动词时优先用 reconnect_* 系列；说"看看/状态/有没有挂"
   等观察类动词时用 check_* 系列。两者不要级联调用 —— check 已经会附带恢复。
```

## 验收标准

1. 用户在 Telegram 自然语言说"把 5557 重连一下" → LLM 调
   `reconnect_instance(port="5557")`，返回结构化结果。
2. 用户说"把所有掉线的实例都拉起来" → LLM 调 `batch_reconnect_instances()`，
   非可重连状态被正确标记 skipped。
3. `/reconnect` 命令的 Telegram 行为与改动前完全一致（结果列表字段同名、
   `_format_reconnect_results` 不需要改）。
4. obs 平台能看到 tool_started=`reconnect_instance` / `batch_reconnect_instances`
   的事件，metadata 里包含 `kind="reconnect"` 与端口、动作、最终状态。
5. `runner/recovery.py` 内的步级自动恢复行为不变（仍直接调 `try_reconnect`）。

## 风险与注意事项

- **LLM 误调用**：若 LLM 在主界面正常的实例上调 `reconnect_instance`，helper 会
  返回 `skipped`，不会破坏状态；不需要额外护栏。
- **批量耗时**：N 个实例 × 最长 60s = 60N 秒，期间 Agent 回合阻塞。系统提示
  里已写明"实例数多时耗时较长"，提醒 LLM 不要在快速对话场景里盲目批量。
- **与 `_task_running` 互斥**：`/reconnect` 命令路径设了 `_task_running` 锁，
  避免任务执行中再触发重连。Tool 路径**不**接入这个锁 —— 因为 Tool 由 LLM
  在 Agent 回合内调用，本来就和 `_run_task_sync` 互斥（同一个 bot 进程的
  `asyncio.to_thread` 串行）。如果未来允许并发任务，这条假设要重新审视。
- **`reconnect_one_port` 的返回格式是新增契约**：obs adapter 当前没有消费它，
  无需联动；但若以后写新看板，请直接读 tool_completed 里 `attach_tool_meta`
  上报的字段。

## 不做的事

- 不为新工具加 timeout 参数（避免 LLM 乱填）。
- 不并发执行批量重连。
- 不改 `diagnose_instance` 的隐式重连逻辑（与新 tool 形成"体检"vs"恢复"的
  互补，不需要合并）。
- 不新增 `reconnect_*` 类的 Telegram inline 按钮 —— "🔌 掉线重连"已经存在。

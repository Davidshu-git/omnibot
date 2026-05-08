# 方案：把"重连实例"作为独立工具暴露给 LLM

> v2.1 修订版。覆盖 v1 / v2。

## ChangeLog

### v2.1（基于自审 review）

1. **核实 `RunnerContext` 直构合法**：dataclass 非默认字段只有 `executor / port`，`check_instance_health` 已在用（`game_tools.py:103 / 140`）。新工具沿用同一风格，**不要**改成 `build_context(...)`。
2. **统一 obs `kind` 命名**：单 / 批量都用 `kind="reconnect"`，多端口附加 `batch_size: int`，避免聚合查询时漏 OR 一个 kind。
3. **采集 `try_reconnect` 的 `steps`**：helper 构造 steps 列表传入 `try_reconnect`，结果一并塞进 `attach_tool_meta`，对齐 `instance_diagnosis` 的 steps 字段，让 obs 看板能展开"诊断过程"。
4. **批量耗时硬兜底**：tool 内对"留空 + 实例数 > 5"的调用直接返回引导话术，避免 LLM 触发 5 分钟阻塞 / Telegram UX 雪崩。
5. **`check_instance_health` 输出标签化**：每行追加 `[需恢复]` / `[正常]`，让 LLM 二阶段决策摆脱 emoji 推断。
6. **prompt 准则 7 加豁免**：用户明确要求"全部重连"时允许直接全量，消除与准则 8 的措辞冲突。
7. **codex 自测指令具体化**：给到 `docker compose` 和 `docker logs` 实际命令。

### v2（相对 v1）

1. **事实修正**：v1 提到的 `batch_check_all_instances` **不存在**。`check_instance_health` 的 `port` 参数已经一并支持空串（=全部）、CSV（=多个）、单端口三种调用。
2. **事实修正**：v1 称 `check_instance_health` 会"隐式调用 `try_reconnect`"——错。`game_tools.py:103 / 140` 调 `diagnose_instance(ctx)` 没传 `attempt_reconnect=True`，**当前是纯只读**。docstring 也写明"纯只读，不执行修复或重连"。
3. **设计简化**：合并 v1 的 `reconnect_instance` + `batch_reconnect_instances` 为单工具 `reconnect_instances(ports="")`，参数风格与 `check_instance_health` 对称。
4. **obs 统一**：`events.reconnect_port` 上移到 helper 内 emit，让按钮路径与 LLM 路径共用同一组 runner 事件。
5. **prompt 现状补齐**：当前 `GAME_SYSTEM_PROMPT` 完全没列 `check_instance_health`——本次需把"诊断 + 恢复"两族整体补进去。

---

## 背景

- mhxy_bot 主控台「📊 实例状态」「🔌 掉线重连」按钮走的是 `tg_main.py::_instance_status_sync` / `_reconnect_sync`，**不经过 LLM**——`runner/perception` + `runner/instance_recovery.try_reconnect` 的确定性流水线。
- 当用户在对话里自然语言说"看看所有实例情况，处理下有问题的实例"——LLM 只能调 `check_instance_health`（纯只读体检）+ 自己用 `sense_screen` / `tap_*` 在线拼装恢复循环，慢、贵、且复制 `try_reconnect` 已有逻辑。
- 合理分工：**LLM 做决策（要不要修、修哪些），脚本做动作（恢复流程）**。要让 LLM 直接调"重连"，必须把 `try_reconnect` 包成 LangChain `@tool`。

## 目标

把"重连实例"提升为 LLM 可主动调用的一类 tool，与 `check_instance_health` 平级。重连仍走 `runner/instance_recovery.try_reconnect`，**不引入新的恢复路径**；同时把 `_reconnect_sync` 折叠到同一 helper，避免双份逻辑漂移。

---

## 改动范围（4 个文件）

### 1) `mhxy_bot/runner/instance_recovery.py`：新增常量与 helper

把 `_reconnect_sync` 的"按当前状态决定 skip / reconnect"逻辑下沉到 helper，并由 helper 负责发 `events.reconnect_port`，让所有重连入口共用同一组事件；同时采集 `try_reconnect` 的 `steps`。

```python
from mhxy_bot.runner.models import InstanceState

# 当前 state 处于这些值时，try_reconnect 才有意义；按钮 / LLM 两条路径都从这里读
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

    内部会 emit `events.reconnect_port`（若 ctx 带 observer），所以调用方不应再
    重复 emit，避免 obs 双写。

    Returns:
        dict 形如:
        - {"action": "skipped",     "state": "<initial>",          "steps": []}
        - {"action": "reconnected", "initial_state": "<s>",
            "final_state": "<s>", "steps": [...]}
        - {"action": "failed",      "initial_state": "<s>",
            "final_state": "<s>", "steps": [...]}
    """
    from mhxy_bot.runner.perception import detect_screen_state
    from mhxy_bot.runner import events

    state = detect_screen_state(ctx)
    if state not in RECONNECTABLE_STATES:
        events.reconnect_port(ctx, state.value, None, state.value)
        return {"action": "skipped", "state": state.value, "steps": []}

    steps: list[str] = []
    ok = try_reconnect(ctx, timeout_sec=timeout_sec, steps=steps)
    final = detect_screen_state(ctx).value
    events.reconnect_port(ctx, state.value, ok, final)
    return {
        "action": "reconnected" if ok else "failed",
        "initial_state": state.value,
        "final_state": final,
        "steps": steps,
    }
```

`try_reconnect` 自身不动（已支持 `steps` 形参，会塞入"自动恢复成功，回到主界面"或"自动恢复失败或超时"等条目）。

### 2) `mhxy_bot/tg_main.py::_reconnect_sync`：折叠到 helper

行为完全等价；目的是让 helper 成为**唯一的重连入口**。

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
        # helper 内部已经 emit events.reconnect_port，这里只负责拼对外结果
        if outcome["action"] == "skipped":
            results.append({"port": port, "action": "skipped",
                            "state": outcome["state"]})
        else:
            results.append({
                "port": port,
                "action": outcome["action"],
                "final_state": outcome["final_state"],
            })
    return results
```

`_format_reconnect_results` 用到的字段名（`action / state / final_state`）保持不变，**HTML 渲染不需要改**。

### 3) `mhxy_bot/tools/game_tools.py`：新增 `reconnect_instances`

参数风格与 `check_instance_health` 完全对称（空串=全部、CSV=多个、单端口=单个）。批量内部直接用 helper 顺序循环，**不要走 `tool.invoke` 互调**（会被 LangChain callback 当成嵌套 tool call，污染 obs 事件流）。

> **`RunnerContext` 直构合法性已核实**：dataclass 非默认字段只有 `executor / port`，`check_instance_health` 现有代码（`game_tools.py:103 / 140`）已经直构。新工具保持同一风格，**不要**改用 `build_context`——LLM 路径没有 observer / trace_id，`build_context` 内的 `executor.set_context(...)` 没增益。
>
> helper 内 `events.reconnect_port` 走 `_emit`，无 observer 时只写 logging（见 `runner/events.py::_emit`）；LLM 路径完全靠 LangChain callback + `attach_tool_meta`，runner 事件只在按钮路径写 obs。

```python
@tool
def reconnect_instances(ports: str = "") -> str:
    """主动对一个或多个模拟器实例执行重连恢复（处理掉线 / 登录界面 / 安卓桌面）。
    若实例当前不在可重连状态（如已在主界面），会被标记为 skipped。
    每个实例最多等待 60 秒，N 个实例最长耗时约 60×N 秒。

    Args:
        ports: 端口号（如 "5557"）、逗号分隔多端口（如 "5557,5559"），
               或留空对所有实例执行重连。

    Returns:
        单实例时返回详细结果；多实例时返回汇总（成功 / 失败 / 跳过统计 + 各实例结果）。
    """
    try:
        from core.observability import attach_tool_meta
        from mhxy_bot.runner.context import RunnerContext
        from mhxy_bot.runner.instance_recovery import reconnect_one_port

        if ports.strip():
            port_list = [p.strip() for p in ports.split(",") if p.strip()]
        else:
            port_list = [str(inst["port"]) for inst in _load_instances().get("instances", [])]
        if not port_list:
            return "❌ 没有可用实例"

        # 软上限：留空且实例数 > 5 时拒绝无差别全量，避免 LLM 触发 60×N 秒阻塞
        BATCH_LIMIT = 5
        if not ports.strip() and len(port_list) > BATCH_LIMIT:
            return (
                f"⚠️ 检测到 {len(port_list)} 个实例。无差别批量重连预计耗时 "
                f">{60 * BATCH_LIMIT}s，建议先调 check_instance_health 筛出 "
                f"`needs_human=True` 实例，再用 ports 参数显式指定端口。"
            )

        # 单端口：返回详细结果
        if len(port_list) == 1:
            port_str = _port_to_str(port_list[0])
            ctx = RunnerContext(executor=executor, port=port_str)
            outcome = reconnect_one_port(ctx, timeout_sec=60)
            attach_tool_meta({
                "kind": "reconnect",
                "port": port_str,
                **outcome,
            })
            action = outcome["action"]
            if action == "skipped":
                return (f"⏭️ 端口 {port_list[0]} 跳过重连\n"
                        f"  当前状态：{outcome['state']}（无需恢复）")
            prefix = "✅" if action == "reconnected" else "❌"
            verb = "成功" if action == "reconnected" else "失败"
            steps_section = ""
            if outcome.get("steps"):
                steps_text = "\n".join(f"  {s}" for s in outcome["steps"])
                steps_section = f"\n【恢复过程】\n{steps_text}"
            return (
                f"{prefix} 端口 {port_list[0]} 重连{verb}\n"
                f"  初始状态：{outcome['initial_state']}\n"
                f"  最终状态：{outcome['final_state']}"
                f"{steps_section}"
            )

        # 多端口：顺序执行 + 汇总
        ok = bad = skip = 0
        lines = []
        batch_results = []
        for p in port_list:
            port_str = _port_to_str(p)
            try:
                ctx = RunnerContext(executor=executor, port=port_str)
                outcome = reconnect_one_port(ctx, timeout_sec=60)
            except Exception as exc:
                outcome = {"action": "failed", "initial_state": "error",
                           "final_state": f"error: {type(exc).__name__}",
                           "steps": []}
            action = outcome["action"]
            if action == "reconnected":
                emoji, detail = "✅", outcome["final_state"]
                ok += 1
            elif action == "failed":
                emoji, detail = "❌", outcome["final_state"]
                bad += 1
            else:  # skipped
                emoji, detail = "⏭️", outcome["state"]
                skip += 1
            lines.append(f"  {emoji} 端口 {p}  {detail}")
            batch_results.append({"port": port_str, **outcome})

        attach_tool_meta({
            "kind": "reconnect",            # 单 / 批量统一同一 kind
            "batch_size": len(port_list),
            "ok": ok,
            "failed": bad,
            "skipped": skip,
            "results": batch_results,
        })
        return (f"共重连 {len(port_list)} 个实例"
                f"（{ok} 成功 / {bad} 失败 / {skip} 跳过）：\n"
                + "\n".join(lines))
    except Exception as e:
        return f"❌ 重连异常：{type(e).__name__} - {e}"
```

`make_game_tools` 返回列表把它加到 `check_instance_health` 之后（"诊断"族紧挨"恢复"族）：

```python
return [
    get_instances,
    batch_recognize_schools,
    check_instance_health,
    reconnect_instances,           # ← 新增
    capture_screenshot,
    sense_screen,
    analyze_scene,
    locate_element_vl,
    tap_coordinate,
    batch_tap_coordinate,
    tap_saved_element,
    press_back,
    batch_press_back,
    list_element_library,
    save_to_element_library,
    delete_from_element_library,
]
```

#### 顺手补：`check_instance_health` 批量输出标签化

让 LLM 摆脱"靠 emoji 推断 needs_human"。改 `game_tools.py:152` 那行：

```python
# 改前
lines.append(f"  {emoji} 端口 {p}  {d['state']}  {d['code']}")
# 改后
tag = "[需恢复]" if (d["needs_human"] or d["code"] != "unknown_ok") else "[正常]"
lines.append(f"  {emoji} 端口 {p}  {d['state']}  {d['code']}  {tag}")
```

`attach_tool_meta` 字段不动，纯文本侧增量。

### 4) `mhxy_bot/tg_main.py::get_tool_status_map`

新增"正在……"文案：

```python
"reconnect_instances":   "🔌 正在重连模拟器实例（最长每实例 60s，请耐心等待）...",
"check_instance_health": "🩺 正在诊断模拟器实例...",
```

> 现状 grep `tg_main.py` 没看到 `check_instance_health` 的状态文案；顺手补齐让 Telegram UX 一致。

### 5) `mhxy_bot/agent.py::GAME_SYSTEM_PROMPT`

当前 prompt 的"## 你的能力"小节根本**没列诊断 / 恢复族**。补成：

```text
- 实例诊断（只读）：单端口或批量体检（check_instance_health，port 留空=全部，
  传 "5557,5559" 可指定多个）—— 仅做 ADB / 截图 / OCR / 屏幕状态判定，
  不点击、不修复。输出每行带 [需恢复] / [正常] 标签。
- 实例恢复（动作）：主动重连一个或多个实例（reconnect_instances，
  ports 留空=全部）—— 对掉线 / 登录界面 / 安卓桌面状态执行恢复脚本，
  每实例最长 60s。
```

并在"## 操作准则"里追加：

```text
7. 处理"看看实例情况，把有问题的拉起来"这类组合诉求时：
   先调 check_instance_health 拿到全局诊断，再依据 [需恢复] 标签的端口
   主动调 reconnect_instances 并以 ports 参数指定它们；
   除非用户明确要求"全部重连/全量恢复"，否则不要无差别批量。
8. 用户说"重连/恢复/拉起"等动作动词时直接用 reconnect_instances；
   说"看看/状态/有没有挂"等观察动词时用 check_instance_health。
   两者职责清晰，不要在同一轮里反复来回调用。
```

---

## 验收标准

1. **新增对话能力**：用户说"把 5557 重连一下" → LLM 调 `reconnect_instances(ports="5557")`，返回带"恢复过程"列表的详细结果。
2. **组合意图**：用户说"看看所有实例情况，处理下有问题的实例" → LLM 先调 `check_instance_health(port="")` 拿到带 `[需恢复]` / `[正常]` 标签的汇总，再对 `[需恢复]` 端口调 `reconnect_instances(ports="<csv>")`，**不会**对正常实例发起重连。
3. **批量软上限**：用户说"全部重连一下" 且当前实例数 > 5 → LLM 调 `reconnect_instances()` 时 tool 直接返回引导话术，**不阻塞 60×N 秒**。LLM 应顺势改用 `check_instance_health` 先筛选。
4. **按钮行为不回归**：
   - `/reconnect` 命令的 Telegram 输出与改动前完全一致（`_format_reconnect_results` 字段同名，文案不变）。
   - 主控台「🔌 掉线重连」按钮行为不变。
5. **obs 事件**：
   - 按钮路径：`scan_started` + 每端口 `reconnect_result`（由 helper 内 emit；schema 不变）。
   - LLM 路径：`tool_started=reconnect_instances` / `tool_completed`，metadata 含 `kind="reconnect"` + 单端口字段或 `batch_size / ok / failed / skipped / results`。
6. **`runner/recovery.py` 内的步级自动恢复行为不变**（仍直接调 `try_reconnect`）。
7. **新代码遵守仓库规范**：
   - 完整 Type Hints + Google 风格 docstring（`Args` / `Returns` / `Raises`）。
   - 异常捕获具体到子类，禁止裸 `except:`。
   - 不引入新第三方依赖。

---

## 风险与注意事项

- **LLM 误触发**：在主界面正常的实例上调 `reconnect_instances` 会被 helper 标记为 `skipped`，不会破坏状态；不需要额外护栏。
- **批量阻塞**：`reconnect_instances` 内置 `BATCH_LIMIT=5` 软上限只对"留空全量"路径生效；用户显式传 6 个端口的 CSV 时仍会跑满 60×N 秒（这是用户明确意图，符合预期）。Telegram "正在输入" 心跳由 `keep_typing_action` 维持。
- **与按钮路径并发**：`/reconnect` 命令路径有 `_task_running` 锁，但 LLM tool 路径**不**接入这个锁——理由是同一个 bot 进程的 `asyncio.to_thread` 串行，本来就不会和 `_run_task_sync` 真并发。两个 Telegram 用户同时操作（一个点按钮、一个发自然语言）属既存风险，本方案不扩大也不收紧。
- **helper emit 的 ctx 来源**：按钮路径构造 ctx 时挂了 `observer` + `trace_id`，`events.reconnect_port` 落到 obs；LLM 路径用裸 `RunnerContext(executor=..., port=...)`，事件只走 `_emit` 的 logging 分支。这是符合预期的——LLM 路径靠 `attach_tool_meta` 写到 tool_completed。
- **`reconnect_one_port` 字段契约**：`action / state / initial_state / final_state / steps` 是新增对外契约，被 `_reconnect_sync` 与新 tool 共用。obs adapter (`obs/api/app/adapters/omnibot_jsonl.py`) 当前消费的是 `reconnect_result` 事件 schema 与 tool meta，不读 helper 返回值，无需联动改 adapter。

---

## 不做的事

- 不为新工具暴露 `timeout_sec` 参数（避免 LLM 乱填；按钮路径与 LLM 路径都硬编码 60s）。
- 不并发执行批量重连（ADB / OCR 顺序读屏，并发会触发 executor 限流）。
- 不改 `diagnose_instance` 的 `attempt_reconnect` 默认值（保持 `check_instance_health` 纯只读语义；恢复职责由新 tool 独立承担）。
- 不新增 Telegram inline 按钮（「🔌 掉线重连」已存在）。
- 不写新单测脚本作为方案落地的硬要求；如需要回归，按 CLAUDE.md 容器内 pytest 流程补即可。

---

## codex 实施 checklist

- [ ] `mhxy_bot/runner/instance_recovery.py`：新增 `RECONNECTABLE_STATES` 常量 + `reconnect_one_port` helper（含 `events.reconnect_port` emit 与 `steps` 采集）。
- [ ] `mhxy_bot/tg_main.py::_reconnect_sync`：替换为 helper 调用，删除原本的状态判断与 `events.reconnect_port` 调用（避免 helper 双写）。
- [ ] `mhxy_bot/tools/game_tools.py`：
  - 在 `make_game_tools` 内新增 `reconnect_instances` `@tool`，加入返回列表。
  - 修改 `check_instance_health` 批量分支输出，每行追加 `[需恢复] / [正常]` 标签。
- [ ] `mhxy_bot/tg_main.py::get_tool_status_map`：补 `reconnect_instances` 与 `check_instance_health` 文案。
- [ ] `mhxy_bot/agent.py::GAME_SYSTEM_PROMPT`：补诊断 / 恢复族能力描述 + 操作准则 7、8。
- [ ] **回归自测**（容器内，参考 CLAUDE.md 约束）：
  ```bash
  # 重建并重启 mhxy bot
  docker compose up -d --build mhxy-tg-bot
  # 跟踪日志确认启动无异常
  docker compose logs -f mhxy-tg-bot --tail=100
  ```
  随后在 Telegram 端：
  1. 点「🔌 掉线重连」按钮，确认输出格式与改动前一致。
  2. 对话发"把所有实例情况看一下，挂了的拉起来"，观察日志：
     LLM 应先调 `check_instance_health(port="")`，再针对带 `[需恢复]` 的端口调
     `reconnect_instances(ports="<csv>")`，不无差别全量。
  3. 对话发"全部重连一下"（实例数 > 5 时），确认 tool 返回引导话术且未阻塞 60×N 秒。
  4. obs 看板（http://localhost:3100）打开 mhxy 会话，确认能看到
     `tool_started=reconnect_instances` 与 `kind="reconnect"` 的 metadata。

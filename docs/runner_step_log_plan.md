# 方案：诊断过程透传给大模型（修订版）

## 背景

`check_instance_health` tool 当前只把最终结论返回给 LLM，诊断的中间过程
（ADB 检查、截图检查、OCR 检查、屏幕状态识别、重连尝试）全部走
`ctx.info()` 进入 Docker logging，LLM 完全不可见。

目标：让 LLM 在 tool 返回值中看到完整的诊断推理链。

---

## 设计决策（与 v1 的差异）

**v1 把 `step_log` 挂在 `RunnerContext` 上**，被否决。理由：

- `RunnerContext` 被 actions / perception / engine / instance_recovery 共享，
  step_log 是诊断业务专属字段，挂上去属于 API 污染。
- 未来若出现长生命周期 ctx（连续任务流），step_log 会无限增长，
  生命周期约束只能靠"每次新建 ctx"的隐式协定维持。

**v2 改为把 `steps: list[str]` 放到 `InstanceDiagnosis` model 上**：

- 数据归属于产生它的那次诊断，与 `code/state/message/details` 同级。
- 任何调用 `diagnose_instance` 的地方（包括未来的自动恢复 watchdog）
  都能直接拿到诊断过程，无需共享上下文。
- `RunnerContext` 保持纯净，不引入方法/字段。

---

## 改动范围（3 个文件）

### 1. `mhxy_bot/runner/models.py`

在 `InstanceDiagnosis` dataclass 中新增 `steps` 字段：

```python
@dataclass
class InstanceDiagnosis:
    code: InstanceIssue
    state: InstanceState
    needs_human: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)   # 新增

    def as_dict(self) -> dict[str, Any]:
        # 同步把 steps 输出（如果 as_dict 已存在）
        ...
```

如有 `as_dict` 之类序列化方法，同步把 `steps` 加进去。

---

### 2. `mhxy_bot/runner/instance_recovery.py`

`diagnose_instance` 内用一个本地 `steps: list[str]` 累加诊断过程，
最后构造 `InstanceDiagnosis` 时把 `steps=steps` 传进去。

#### 关键约束

- **全部使用中文**，与 LLM 阅读方对齐；现有英文 `ctx.info()` 也一并改成中文。
- **steps 只记"过程"，不记"结论"**：结论已经在 `message` 字段里，
  最后一行不再重复 `instance usable, state=main_ui` 这种话。
- **单次诊断 steps 不超过 12 条**：防止 token 膨胀；超出时截断尾部并
  追加一条 `…（已截断）`。
- `try_reconnect` 的 30 次轮询心跳**不进 steps**，仍走 `ctx.info()`。
  只有 reconnect 的入口、成功、超时三个事件进 steps。

#### 改造点（按执行顺序）

| 位置 | 进 steps 的事件 |
|------|----------------|
| `diagnose_instance` 入口异常 | "app_health 调用失败：{exc}" |
| ADB 分支 | "ADB 未连接" |
| screenshot 分支 | "截图失败" |
| OCR available 分支 | "OCR 不可用" |
| sense 调用失败 | "OCR sense 调用失败：{exc}" |
| sense 调用成功 | "ADB / 截图 / OCR 均正常" *(三项合一记录，避免噪声)* |
| `detect_screen_state` 后 | "屏幕状态识别：{state.value}" |
| LOGIN_SCREEN 进入重连 | "检测到登录界面，尝试自动进入游戏" |
| DISCONNECTED 进入重连 | "检测到掉线，尝试自动重连" |
| try_reconnect 返回 True | *由 try_reconnect 写入*："自动恢复成功，回到主界面" |
| try_reconnect 返回 False | *由 try_reconnect 写入*："自动恢复失败或超时（{N}s）" |

#### `try_reconnect` 签名调整

让 `try_reconnect` 也能往 steps 里写。最简单的做法：把 `steps` 作为可选参数传入。

```python
def try_reconnect(
    ctx: "RunnerContext",
    timeout_sec: int = 90,
    steps: list[str] | None = None,
) -> bool:
    ...
    # 成功
    ctx.info("reconnect: success, back to main UI")
    if steps is not None:
        steps.append("自动恢复成功，回到主界面")
    return True
    ...
    # 超时
    ctx.warning("reconnect: timeout after %ds", timeout_sec)
    if steps is not None:
        steps.append(f"自动恢复失败或超时（{timeout_sec}s）")
    return False
```

`diagnose_instance` 调用处：

```python
if state == InstanceState.LOGIN_SCREEN:
    steps.append("检测到登录界面，尝试自动进入游戏")
    if try_reconnect(ctx, steps=steps):
        ...
```

#### 异常分支 steps 的处理

每个 `return InstanceDiagnosis(...)` 之前，把当前累计的 `steps` 一并传进去。
例如 ADB 分支：

```python
if not health.get("adb"):
    steps.append("ADB 未连接")
    return InstanceDiagnosis(
        code=InstanceIssue.ADB_OFFLINE,
        state=InstanceState.OFFLINE,
        needs_human=True,
        message="ADB is not connected",
        details=details,
        steps=steps,
    )
```

#### dry_run 分支

直接构造空 `steps=[]`，保持当前行为不变。

---

### 3. `mhxy_bot/tools/game_tools.py`

#### 主路径

在 `check_instance_health` 的成功返回前，根据 `diag.steps` 拼接段落：

```python
diag = diagnose_instance(ctx)
d = diag.as_dict()

# ...prefix 判断逻辑保持不变...

steps_section = ""
if diag.steps:
    steps_text = "\n".join(f"  {s}" for s in diag.steps)
    steps_section = f"\n【诊断过程】\n{steps_text}"

return (
    f"{prefix}端口 {port} 健康诊断\n"
    f"  状态：{state}\n"
    f"  问题码：{code}\n"
    f"  需要人工介入：{'是' if needs_human else '否'}\n"
    f"  说明：{message}"
    f"{steps_section}"
)
```

#### 异常路径也输出 steps

外层 `try/except` 在 `diagnose_instance` 自身抛出时会丢失已积累的 steps。
做法：把 `steps` 提到 try 之前作为本地变量是不可行的（它在 `diagnose_instance`
内部）。**改用更稳的方式**：在 `diagnose_instance` 内部捕获所有可能抛出的异常，
统一以 `InstanceDiagnosis(code=..., steps=steps)` 返回，让 `check_instance_health`
的外层 `except` 只捕获真正"框架级"异常（导入失败、参数非法等）。

具体改动：`diagnose_instance` 已经在做大部分异常包装，只需在最外层
再加一个兜底 `try/except Exception` 即可，把任何意外异常转换为
`InstanceDiagnosis(code=ADB_OFFLINE 或新建 INTERNAL_ERROR, steps=steps, ...)`。

> 如果不想新增 issue 枚举，可以直接复用 `UNKNOWN_OK=False`（即 needs_human=True）
> 加一条 `f"诊断内部异常：{exc}"` step 即可。

`check_instance_health` 外层 `except` 保留，仅作为最后兜底。

#### `batch_check_all_instances` 不变

批量摘要只取首行，steps 对汇总无意义。

---

## 约束（强制执行）

- 不改动 `runner/actions.py`、`runner/perception.py`、`runner/engine.py`、`runner/context.py`
- 不改 `agent.py`
- step 文案全部中文，与 LLM 阅读方对齐
- step 只记过程，不记结论（结论在 `message` 字段）
- 单次诊断 steps 上限 12 条，超出截断
- `try_reconnect` 的轮询心跳不进 steps，保持 `ctx.info()`

---

## 单元测试（必做）

新增 `tests/test_instance_recovery_steps.py`（或追加到现有测试文件），
至少覆盖三个用例：

1. **ADB 离线**：mock `ctx.executor.app_health` 返回 `{"adb": False, ...}`，
   断言 `diag.steps` 非空、包含 "ADB 未连接"。
2. **OCR 异常**：mock `ctx.executor.sense` 抛异常，断言 steps 含 "OCR sense 调用失败"。
3. **正常路径**：mock 全部健康 + `detect_screen_state` 返回 MAIN_UI，
   断言 steps 含 "屏幕状态识别：main_ui"，且不含 message 同义重复行。

---

## 验证方式

1. 跑单元测试：`pytest tests/test_instance_recovery_steps.py -v`
2. 重启容器：`docker compose restart mhxy-tg-bot`（或对应服务名）
3. 在 Telegram 中让大模型调用 `check_instance_health`，
   返回内容应包含 `【诊断过程】` 段落，列出诊断各步骤，全中文，无与 `说明：` 字段重复的行。

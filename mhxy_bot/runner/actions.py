"""动作执行层：将 TaskStep.action 映射到 ExecutorClient 调用。"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mhxy_bot.runner.context import RunnerContext
    from mhxy_bot.runner.models import TaskStep

# 返回值约定：成功返回 dict（可为空），失败抛出 ActionError
class ActionError(Exception):
    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


# ---------------------------------------------------------------------------
# 各 action 实现
# ---------------------------------------------------------------------------

def _tap_element(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """从元素库读取坐标并点击。"""
    if ctx.dry_run:
        ctx.info("[dry_run] tap_element element=%s", step.element)
        return {}
    if not step.element:
        raise ActionError("tap_element 需要 element 字段")
    try:
        from mhxy_bot.game_core.element_library import get_element_library
    except ImportError as e:
        raise ActionError(f"element_library 不可用: {e}") from e

    lib = get_element_library()
    elem = lib.get_element(step.element)
    if not elem:
        raise ActionError(f"元素库中未找到元素 '{step.element}'")

    x, y = float(elem["x"]), float(elem["y"])
    from mhxy_bot.config import DEFAULT_RESOLUTION
    W, H = DEFAULT_RESOLUTION
    px = int(x * W)
    py = int(y * H)
    ok = ctx.executor.tap(ctx.port, px, py)
    if not ok:
        raise ActionError(f"tap_element '{step.element}' ADB 返回失败")
    return {"element": step.element, "px": px, "py": py}


def _tap_text(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """OCR 找文本并点击。text 或 text_any 必填一个。"""
    if ctx.dry_run:
        ctx.info("[dry_run] tap_text candidates=%s", step.text_any or [step.text])
        return {}
    candidates = step.text_any or ([step.text] if step.text else [])
    if not candidates:
        raise ActionError("tap_text 需要 text 或 text_any 字段")

    result = ctx.executor.tap_text(ctx.port, candidates)
    if not result.get("found"):
        raise ActionError(f"tap_text 未找到候选文字 {candidates}")
    return {"matched": result.get("text"), "px": result.get("px"), "py": result.get("py")}


def _wait_text(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """等待文本出现。text 或 text_any 必填一个。"""
    if ctx.dry_run:
        ctx.info("[dry_run] wait_text candidates=%s timeout=%s",
                 step.text_any or [step.text], step.timeout_sec)
        return {}
    candidates = step.text_any or ([step.text] if step.text else [])
    if not candidates:
        raise ActionError("wait_text 需要 text 或 text_any 字段")

    result = ctx.executor.wait_text(
        ctx.port, candidates,
        timeout_sec=step.timeout_sec,
        interval_sec=step.extra.get("interval_sec", 1.5),
    )
    if not result.get("found"):
        raise ActionError(f"wait_text 超时未出现 {candidates}")
    return {"matched": result.get("text"), "px": result.get("px"), "py": result.get("py")}


def _wait_not_battle(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """等待战斗从进入到结束。

    两阶段语义：
    1. 先等待进入战斗，或直接出现奖励/继续/确定等终态文本。
    2. 如果已进入战斗，再等待战斗状态稳定消失或终态文本出现。
    """
    from mhxy_bot.runner.perception import detect_screen_state, has_text
    from mhxy_bot.runner.models import InstanceState

    if ctx.dry_run:
        ctx.info("[dry_run] wait_not_battle timeout=%s", step.timeout_sec)
        return {}

    # Keep defaults strict. Text like "继续"/"挑战"/"秘境降妖" can also appear
    # before battle starts, so treating it as an early terminal signal would
    # let this step complete while the fight is still loading.
    terminal_texts = step.text_any or step.extra.get("terminal_text_any") or [
        "获得奖励", "奖励", "战斗胜利", "通关", "下一关", "确定"
    ]
    interval = float(step.extra.get("interval_sec", 1.5))
    enter_timeout = int(step.extra.get("enter_timeout_sec", min(60, step.timeout_sec)))

    entered_battle = False
    first_deadline = time.monotonic() + enter_timeout
    while time.monotonic() < first_deadline:
        if ctx.stop_requested:
            raise ActionError("wait_not_battle stopped before battle entry")
        if has_text(ctx, terminal_texts):
            return {"entered_battle": False, "terminal": True}
        state = detect_screen_state(ctx)
        if state == InstanceState.IN_BATTLE:
            entered_battle = True
            break
        if state in (InstanceState.OFFLINE, InstanceState.LOGIN_SCREEN, InstanceState.DISCONNECTED):
            raise ActionError(f"wait_not_battle abnormal state before battle: {state.value}")
        time.sleep(interval)

    if not entered_battle:
        raise ActionError(
            f"wait_not_battle 未在 {enter_timeout}s 内进入战斗或出现终态文本 {terminal_texts}"
        )

    deadline = time.monotonic() + max(1, step.timeout_sec - enter_timeout)
    stable_not_battle = 0
    while time.monotonic() < deadline:
        if ctx.stop_requested:
            raise ActionError("wait_not_battle stopped while waiting for battle end")
        if has_text(ctx, terminal_texts):
            return {"entered_battle": True, "terminal": True}
        state = detect_screen_state(ctx)
        if state == InstanceState.IN_BATTLE:
            stable_not_battle = 0
        elif state in (InstanceState.OFFLINE, InstanceState.LOGIN_SCREEN, InstanceState.DISCONNECTED):
            raise ActionError(f"wait_not_battle abnormal state during battle: {state.value}")
        else:
            stable_not_battle += 1
            if stable_not_battle >= 2:
                return {"entered_battle": True, "terminal": False, "state": state.value}
        time.sleep(interval)

    raise ActionError(f"wait_not_battle 超时 ({step.timeout_sec}s)，战斗未确认结束")


def _close_common_popups(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """关闭常见弹窗。"""
    if ctx.dry_run:
        ctx.info("[dry_run] close_common_popups")
        return {}
    result = ctx.executor.close_common_popups(ctx.port)
    return {"closed": result.get("count", 0)}


def _app_health(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """Run structured instance diagnosis and fail when human action is needed."""
    from mhxy_bot.runner.instance_recovery import diagnose_instance

    diagnosis = diagnose_instance(ctx)
    detail = diagnosis.as_dict()
    if diagnosis.needs_human:
        raise ActionError(
            f"instance diagnosis failed: {diagnosis.code.value} ({diagnosis.message})",
            {"diagnosis": detail},
        )
    return {"diagnosis": detail}


def _detect_screen_state(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """Detect current screen state and reject login/disconnect/offline states."""
    from mhxy_bot.runner.models import InstanceState
    from mhxy_bot.runner.perception import detect_screen_state

    if ctx.dry_run:
        ctx.info("[dry_run] detect_screen_state")
        return {"state": InstanceState.UNKNOWN.value}

    state = detect_screen_state(ctx)
    if state in (InstanceState.OFFLINE, InstanceState.LOGIN_SCREEN, InstanceState.DISCONNECTED):
        raise ActionError(f"screen state requires human action: {state.value}")
    return {"state": state.value}


def _press_back(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """按返回键。"""
    if ctx.dry_run:
        ctx.info("[dry_run] press_back")
        return {}
    ok = ctx.executor.back(ctx.port)
    if not ok:
        raise ActionError("press_back ADB 返回失败")
    return {}


def _sleep(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """等待指定秒数。duration_sec 从 extra 或 timeout_sec 取。"""
    duration = float(step.extra.get("duration_sec", step.timeout_sec))
    ctx.info("sleep %.1fs", duration)
    if not ctx.dry_run:
        time.sleep(duration)
    return {"duration_sec": duration}


# ---------------------------------------------------------------------------
# 分发表
# ---------------------------------------------------------------------------

_ACTION_MAP = {
    "app_health":          _app_health,
    "detect_screen_state": _detect_screen_state,
    "tap_element":         _tap_element,
    "tap_text":            _tap_text,
    "wait_text":           _wait_text,
    "wait_not_battle":     _wait_not_battle,
    "close_common_popups": _close_common_popups,
    "press_back":          _press_back,
    "sleep":               _sleep,
}


def execute(ctx: "RunnerContext", step: "TaskStep") -> dict:
    """执行单个 step，返回 detail dict；不处理重试，由 engine 负责。"""
    fn = _ACTION_MAP.get(step.action)
    if fn is None:
        raise ActionError(f"未知 action: '{step.action}'")
    return fn(ctx, step)


def supported_actions() -> list[str]:
    return list(_ACTION_MAP.keys())

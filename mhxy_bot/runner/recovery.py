"""Recovery 策略：step 失败后尝试恢复到可继续的状态。"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from mhxy_bot.runner.models import InstanceState

if TYPE_CHECKING:
    from mhxy_bot.runner.context import RunnerContext


def close_common_popups(ctx: "RunnerContext") -> bool:
    """关闭常见弹窗，返回是否关闭了至少一个。"""
    try:
        result = ctx.executor.close_common_popups(ctx.port)
        count = result.get("count", 0)
        if count:
            ctx.info("recovery: closed %d popup(s)", count)
        return count > 0
    except Exception as exc:
        ctx.warning("recovery close_common_popups failed: %s", exc)
        return False


def press_back(ctx: "RunnerContext", times: int = 1) -> None:
    """按 N 次返回键。"""
    for i in range(times):
        try:
            ctx.executor.back(ctx.port)
            time.sleep(0.5)
        except Exception as exc:
            ctx.warning("recovery press_back[%d] failed: %s", i, exc)


def detect_screen_state(ctx: "RunnerContext") -> InstanceState:
    """在 recovery 上下文中检测屏幕状态。"""
    from mhxy_bot.runner.perception import detect_screen_state as _detect
    try:
        return _detect(ctx)
    except Exception as exc:
        ctx.warning("recovery detect_screen_state failed: %s", exc)
        return InstanceState.UNKNOWN


def return_to_main_ui(ctx: "RunnerContext", max_backs: int = 5) -> bool:
    """尝试回到主界面：先关弹窗，再按返回键，最多 max_backs 次。

    返回是否成功回到主界面。不做自动登录。
    """
    from mhxy_bot.runner.perception import detect_screen_state as _detect

    close_common_popups(ctx)
    time.sleep(0.5)

    for attempt in range(max_backs):
        state = _detect(ctx)
        ctx.info("recovery return_to_main_ui attempt=%d state=%s", attempt, state.value)

        if state == InstanceState.MAIN_UI:
            return True
        if state in (InstanceState.OFFLINE, InstanceState.LOGIN_SCREEN, InstanceState.DISCONNECTED):
            ctx.warning("recovery: instance offline/login/disconnected, cannot auto-recover")
            return False

        close_common_popups(ctx)
        press_back(ctx, times=1)
        time.sleep(1.0)

    state = _detect(ctx)
    if state == InstanceState.MAIN_UI:
        return True
    ctx.warning("recovery return_to_main_ui failed after %d attempts, state=%s",
                max_backs, state.value)
    return False


def mark_needs_human(ctx: "RunnerContext", reason: str) -> None:
    """记录需要人工介入的原因。"""
    ctx.warning("NEEDS_HUMAN port=%s reason=%s", ctx.port, reason)


def attempt(ctx: "RunnerContext", step_id: str, attempt_num: int) -> bool:
    """标准 recovery 流程：关弹窗 → 按返回 → 检测状态。

    返回 True 表示恢复可能成功，可以重试 step；False 表示需要人工。
    """
    ctx.info("recovery attempt #%d for step '%s'", attempt_num, step_id)

    closed = close_common_popups(ctx)
    time.sleep(0.3)

    state = detect_screen_state(ctx)

    if state == InstanceState.DISCONNECTED:
        ctx.info("recovery: disconnected, attempting auto-reconnect for step '%s'", step_id)
        from mhxy_bot.runner.instance_recovery import try_reconnect
        if try_reconnect(ctx):
            ctx.info("recovery: reconnected, retrying step '%s'", step_id)
            return True
        mark_needs_human(ctx, f"reconnect failed after step '{step_id}' disconnect")
        return False

    if state in (InstanceState.OFFLINE, InstanceState.LOGIN_SCREEN):
        mark_needs_human(ctx, f"instance {state.value} after step '{step_id}' failure")
        return False

    if state == InstanceState.POPUP or closed:
        ctx.info("recovery: popup cleared, retrying step '%s'", step_id)
        return True

    if state == InstanceState.IN_BATTLE:
        ctx.info("recovery: in battle, pressing back")
        press_back(ctx, times=2)
        time.sleep(1.0)
        return True

    press_back(ctx, times=1)
    time.sleep(0.5)
    return True

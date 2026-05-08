"""Instance-level diagnosis and lightweight auto-recovery.

diagnose_instance: classify minimum actionable health state (callers decide
  whether to skip or ask for help).
try_reconnect: advance through disconnect/update/launcher/login states and wait
  for main UI; returns False if the game cannot reach main UI before timeout.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from mhxy_bot.runner.models import (
    InstanceDiagnosis,
    InstanceIssue,
    InstanceState,
)

if TYPE_CHECKING:
    from mhxy_bot.runner.context import RunnerContext


def _tap_from_items(ctx: "RunnerContext", items: list[dict], candidates: list[str]) -> bool:
    """在已有 OCR items 中模糊匹配候选文字并点击（一次 OCR 复用）。"""
    for candidate in candidates:
        for item in items:
            if candidate in str(item.get("text", "")):
                return bool(ctx.executor.tap(
                    ctx.port,
                    int(item["center_x"]),
                    int(item["center_y"]),
                ))
    return False


def _tap_text(ctx: "RunnerContext", candidates: list[str]) -> bool:
    """OCR + 模糊匹配候选文字并点击（独立 sense 调用，用于无预取 items 的场景）。"""
    items = ctx.executor.sense(ctx.port)
    return _tap_from_items(ctx, items, candidates)


def try_reconnect(ctx: "RunnerContext", timeout_sec: int = 90, steps: list[str] | None = None) -> bool:
    """掉线后自动重连：处理掉线、更新重启、桌面启动、登录入口，等待主界面。

    Returns True if back to main UI.
    Returns False if the game cannot reach main UI before timeout.
    """
    if ctx.dry_run:
        ctx.info("[dry_run] try_reconnect skipped")
        return True

    reconnect_actions = ["重新登录", "确定"]
    center_tapped = False  # 过场动画点击一次即可

    from mhxy_bot.runner.perception import detect_with_texts

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        state, _, items = detect_with_texts(ctx)          # 一次 OCR
        ctx.info("reconnect: waiting... state=%s", state.value)
        if state == InstanceState.MAIN_UI:
            ctx.info("reconnect: success, back to main UI")
            if steps is not None:
                steps.append("自动恢复成功，回到主界面")
            return True
        if state == InstanceState.DISCONNECTED:
            try:
                if not _tap_from_items(ctx, items, reconnect_actions):
                    ctx.warning("reconnect: disconnect action button not found")
            except Exception:
                pass
        elif state == InstanceState.UPDATE_RESTART:
            try:
                _tap_from_items(ctx, items, ["确定"])
            except Exception:
                pass
        elif state == InstanceState.ANDROID_HOME:
            try:
                _tap_from_items(ctx, items, ["梦幻西游"])
            except Exception:
                pass
        elif state == InstanceState.LOGIN_SCREEN:
            try:
                _tap_from_items(ctx, items, ["登录游戏"])
            except Exception:
                pass
        elif state == InstanceState.ACTIVITY_POPUP:
            try:
                ctx.executor.back(ctx.port)
            except Exception:
                pass
        elif state == InstanceState.POPUP:
            try:
                if not _tap_from_items(ctx, items, ["取消", "关闭", "我知道了"]):
                    ctx.executor.back(ctx.port)
            except Exception:
                pass
        elif state == InstanceState.APP_LOADING:
            if not center_tapped:
                try:
                    ctx.executor.tap(ctx.port, 800, 450)
                    ctx.info("reconnect: tap center to skip transition animation")
                    center_tapped = True
                except Exception:
                    pass
        elif len(items) <= 2 and not center_tapped:
            # UNKNOWN 且几乎认不到字 → 很可能是过场动画
            try:
                ctx.executor.tap(ctx.port, 800, 450)
                ctx.info("reconnect: tap center (low-text unknown screen)")
                center_tapped = True
            except Exception:
                pass
        time.sleep(3.0)

    ctx.warning("reconnect: timeout after %ds", timeout_sec)
    if steps is not None:
        steps.append(f"自动恢复失败或超时（{timeout_sec}s）")
    return False


def diagnose_instance(ctx: "RunnerContext", attempt_reconnect: bool = False) -> InstanceDiagnosis:
    """Classify the minimum actionable health state for one game instance.

    Args:
        ctx: Runner context bound to one emulator instance.
        attempt_reconnect: Whether to actively call try_reconnect for recoverable
            login/disconnect states. Defaults to False so health checks remain
            read-only and do not click or change game state.

    Returns:
        Instance diagnosis with issue code, state, human-action flag, and steps.
    """
    steps: list[str] = []

    if ctx.dry_run:
        return InstanceDiagnosis(
            code=InstanceIssue.UNKNOWN_OK,
            state=InstanceState.UNKNOWN,
            needs_human=False,
            message="dry_run: instance diagnosis skipped",
            steps=steps,
        )

    def _cap_steps() -> None:
        if len(steps) > 12:
            del steps[12:]
            steps.append("…（已截断）")

    try:
        try:
            health = ctx.executor.app_health(ctx.port)
        except Exception as exc:
            steps.append(f"app_health 调用失败：{exc}")
            _cap_steps()
            return InstanceDiagnosis(
                code=InstanceIssue.ADB_OFFLINE,
                state=InstanceState.OFFLINE,
                needs_human=True,
                message=f"app_health error: {exc}",
                steps=steps,
            )

        details = health.get("details") or {}
        if not health.get("adb"):
            steps.append("ADB 未连接")
            _cap_steps()
            return InstanceDiagnosis(
                code=InstanceIssue.ADB_OFFLINE,
                state=InstanceState.OFFLINE,
                needs_human=True,
                message="ADB is not connected",
                details=details,
                steps=steps,
            )
        if not health.get("screenshot"):
            steps.append("截图失败")
            _cap_steps()
            return InstanceDiagnosis(
                code=InstanceIssue.SCREENSHOT_FAILED,
                state=InstanceState.OFFLINE,
                needs_human=True,
                message="screenshot failed",
                details=details,
                steps=steps,
            )
        if not health.get("ocr"):
            steps.append("OCR 不可用")
            _cap_steps()
            return InstanceDiagnosis(
                code=InstanceIssue.OCR_FAILED,
                state=InstanceState.UNKNOWN,
                needs_human=True,
                message="OCR unavailable",
                details=details,
                steps=steps,
            )

        from mhxy_bot.runner.perception import detect_screen_state, detect_with_texts

        state, _, _ = detect_with_texts(ctx)
        steps.append(f"OCR 正常，屏幕状态：{state.value}")
        _cap_steps()

        if state == InstanceState.LOGIN_SCREEN:
            if not attempt_reconnect:
                steps.append("检测到登录界面，未执行自动重连")
                _cap_steps()
                return InstanceDiagnosis(
                    code=InstanceIssue.LOGIN_SCREEN,
                    state=InstanceState.LOGIN_SCREEN,
                    needs_human=True,
                    message="game is at login screen; reconnect not attempted",
                    steps=steps,
                )
            ctx.info("diagnose: login screen, attempting auto-login entry")
            steps.append("检测到登录界面，尝试自动进入游戏")
            _cap_steps()
            if try_reconnect(ctx, steps=steps):
                state = detect_screen_state(ctx)
                _cap_steps()
                reported = InstanceState.UNKNOWN if state == InstanceState.OFFLINE else state
                return InstanceDiagnosis(
                    code=InstanceIssue.UNKNOWN_OK,
                    state=reported,
                    needs_human=False,
                    message=f"entered game successfully, state={reported.value}",
                    steps=steps,
                )
            _cap_steps()
            return InstanceDiagnosis(
                code=InstanceIssue.LOGIN_SCREEN,
                state=InstanceState.LOGIN_SCREEN,
                needs_human=True,
                message="game login entry failed or timed out",
                steps=steps,
            )
        if state in (InstanceState.UPDATE_RESTART, InstanceState.ANDROID_HOME, InstanceState.APP_LOADING):
            _cap_steps()
            return InstanceDiagnosis(
                code=InstanceIssue.LOGIN_SCREEN,
                state=state,
                needs_human=True,
                message=f"instance is not ready for tasks: {state.value}",
                steps=steps,
            )
        if state == InstanceState.DISCONNECTED:
            if not attempt_reconnect:
                steps.append("检测到掉线，未执行自动重连")
                _cap_steps()
                return InstanceDiagnosis(
                    code=InstanceIssue.DISCONNECTED,
                    state=InstanceState.DISCONNECTED,
                    needs_human=True,
                    message="game disconnected; reconnect not attempted",
                    steps=steps,
                )
            ctx.info("diagnose: disconnected, attempting auto-reconnect")
            steps.append("检测到掉线，尝试自动重连")
            _cap_steps()
            if try_reconnect(ctx, steps=steps):
                state = detect_screen_state(ctx)
                _cap_steps()
                reported = InstanceState.UNKNOWN if state == InstanceState.OFFLINE else state
                return InstanceDiagnosis(
                    code=InstanceIssue.UNKNOWN_OK,
                    state=reported,
                    needs_human=False,
                    message=f"reconnected successfully, state={reported.value}",
                    steps=steps,
                )
            _cap_steps()
            return InstanceDiagnosis(
                code=InstanceIssue.DISCONNECTED,
                state=InstanceState.DISCONNECTED,
                needs_human=True,
                message="game disconnected and auto-reconnect failed",
                steps=steps,
            )

        reported_state = InstanceState.UNKNOWN if state == InstanceState.OFFLINE else state
        _cap_steps()
        return InstanceDiagnosis(
            code=InstanceIssue.UNKNOWN_OK,
            state=reported_state,
            needs_human=False,
            message=f"instance usable, state={reported_state.value}",
            details=details,
            steps=steps,
        )
    except Exception as exc:
        steps.append(f"诊断内部异常：{exc}")
        _cap_steps()
        return InstanceDiagnosis(
            code=InstanceIssue.UNKNOWN_OK,
            state=InstanceState.UNKNOWN,
            needs_human=True,
            message=f"internal diagnosis error: {exc}",
            steps=steps,
        )

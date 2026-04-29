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


def _tap_exact_text(ctx: "RunnerContext", candidates: list[str]) -> bool:
    """Tap OCR text only when the recognized text exactly matches a candidate."""
    items = ctx.executor.sense(ctx.port)
    for candidate in candidates:
        for item in items:
            if str(item.get("text", "")).strip() == candidate:
                return bool(ctx.executor.tap(
                    ctx.port,
                    int(item["center_x"]),
                    int(item["center_y"]),
                ))
    return False


def try_reconnect(ctx: "RunnerContext", timeout_sec: int = 90) -> bool:
    """掉线后自动重连：处理掉线、更新重启、桌面启动、登录入口，等待主界面。

    Returns True if back to main UI.
    Returns False if the game cannot reach main UI before timeout.
    """
    if ctx.dry_run:
        ctx.info("[dry_run] try_reconnect skipped")
        return True

    reconnect_actions = ["重新登录", "确定"]

    from mhxy_bot.runner.perception import detect_screen_state

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        state = detect_screen_state(ctx)
        ctx.info("reconnect: waiting... state=%s", state.value)
        if state == InstanceState.MAIN_UI:
            ctx.info("reconnect: success, back to main UI")
            return True
        if state == InstanceState.DISCONNECTED:
            # Dialog may have reappeared after animation; tap again.
            try:
                if not _tap_exact_text(ctx, reconnect_actions):
                    ctx.warning("reconnect: disconnect action button not found")
            except Exception:
                pass
        elif state == InstanceState.UPDATE_RESTART:
            try:
                _tap_exact_text(ctx, ["确定"])
            except Exception:
                pass
        elif state == InstanceState.ANDROID_HOME:
            try:
                _tap_exact_text(ctx, ["梦幻西游"])
            except Exception:
                pass
        elif state == InstanceState.LOGIN_SCREEN:
            try:
                _tap_exact_text(ctx, ["登录游戏"])
            except Exception:
                pass
        elif state == InstanceState.ACTIVITY_POPUP:
            try:
                ctx.executor.back(ctx.port)
            except Exception:
                pass
        elif state == InstanceState.POPUP:
            try:
                if not _tap_exact_text(ctx, ["取消", "关闭", "我知道了"]):
                    ctx.executor.back(ctx.port)
            except Exception:
                pass
        time.sleep(3.0)

    ctx.warning("reconnect: timeout after %ds", timeout_sec)
    return False


def diagnose_instance(ctx: "RunnerContext") -> InstanceDiagnosis:
    """Classify the minimum actionable health state for one game instance."""
    if ctx.dry_run:
        return InstanceDiagnosis(
            code=InstanceIssue.UNKNOWN_OK,
            state=InstanceState.UNKNOWN,
            needs_human=False,
            message="dry_run: instance diagnosis skipped",
        )

    try:
        health = ctx.executor.app_health(ctx.port)
    except Exception as exc:
        return InstanceDiagnosis(
            code=InstanceIssue.ADB_OFFLINE,
            state=InstanceState.OFFLINE,
            needs_human=True,
            message=f"app_health error: {exc}",
        )

    details = health.get("details") or {}
    if not health.get("adb"):
        return InstanceDiagnosis(
            code=InstanceIssue.ADB_OFFLINE,
            state=InstanceState.OFFLINE,
            needs_human=True,
            message="ADB is not connected",
            details=details,
        )
    if not health.get("screenshot"):
        return InstanceDiagnosis(
            code=InstanceIssue.SCREENSHOT_FAILED,
            state=InstanceState.OFFLINE,
            needs_human=True,
            message="screenshot failed",
            details=details,
        )
    if not health.get("ocr"):
        return InstanceDiagnosis(
            code=InstanceIssue.OCR_FAILED,
            state=InstanceState.UNKNOWN,
            needs_human=True,
            message="OCR unavailable",
            details=details,
        )

    try:
        ctx.executor.sense(ctx.port)
    except Exception as exc:
        return InstanceDiagnosis(
            code=InstanceIssue.OCR_FAILED,
            state=InstanceState.UNKNOWN,
            needs_human=True,
            message=f"OCR/sense failed: {exc}",
            details=details,
        )

    from mhxy_bot.runner.perception import detect_screen_state

    state = detect_screen_state(ctx)
    if state == InstanceState.LOGIN_SCREEN:
        ctx.info("diagnose: login screen, attempting auto-login entry")
        if try_reconnect(ctx):
            state = detect_screen_state(ctx)
            reported = InstanceState.UNKNOWN if state == InstanceState.OFFLINE else state
            return InstanceDiagnosis(
                code=InstanceIssue.UNKNOWN_OK,
                state=reported,
                needs_human=False,
                message=f"entered game successfully, state={reported.value}",
            )
        return InstanceDiagnosis(
            code=InstanceIssue.LOGIN_SCREEN,
            state=InstanceState.LOGIN_SCREEN,
            needs_human=True,
            message="game login entry failed or timed out",
        )
    if state in (InstanceState.UPDATE_RESTART, InstanceState.ANDROID_HOME, InstanceState.APP_LOADING):
        return InstanceDiagnosis(
            code=InstanceIssue.LOGIN_SCREEN,
            state=state,
            needs_human=True,
            message=f"instance is not ready for tasks: {state.value}",
        )
    if state == InstanceState.DISCONNECTED:
        ctx.info("diagnose: disconnected, attempting auto-reconnect")
        if try_reconnect(ctx):
            state = detect_screen_state(ctx)
            reported = InstanceState.UNKNOWN if state == InstanceState.OFFLINE else state
            return InstanceDiagnosis(
                code=InstanceIssue.UNKNOWN_OK,
                state=reported,
                needs_human=False,
                message=f"reconnected successfully, state={reported.value}",
            )
        return InstanceDiagnosis(
            code=InstanceIssue.DISCONNECTED,
            state=InstanceState.DISCONNECTED,
            needs_human=True,
            message="game disconnected and auto-reconnect failed",
        )

    reported_state = InstanceState.UNKNOWN if state == InstanceState.OFFLINE else state
    return InstanceDiagnosis(
        code=InstanceIssue.UNKNOWN_OK,
        state=reported_state,
        needs_human=False,
        message=f"instance usable, state={reported_state.value}",
        details=details,
    )

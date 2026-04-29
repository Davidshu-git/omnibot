"""Instance-level diagnosis and lightweight auto-recovery.

diagnose_instance: classify minimum actionable health state (callers decide
  whether to skip or ask for help).
try_reconnect: click "重新登录" and wait for main UI; returns False if the
  game drops to a login screen or times out (needs human).
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


def try_reconnect(ctx: "RunnerContext", timeout_sec: int = 60) -> bool:
    """掉线后自动重连：点击"重新登录"，等待主界面出现。

    Returns True if back to main UI.
    Returns False if game lands on login screen, ADB goes offline, or timeout
    (all require human intervention).
    """
    if ctx.dry_run:
        ctx.info("[dry_run] try_reconnect skipped")
        return True

    ctx.info("reconnect: tapping 重新登录 port=%s", ctx.port)
    try:
        result = ctx.executor.tap_text(ctx.port, ["重新登录"])
        if not result.get("found"):
            ctx.warning("reconnect: 重新登录 button not found")
            return False
    except Exception as exc:
        ctx.warning("reconnect: tap_text error: %s", exc)
        return False

    time.sleep(2.0)

    from mhxy_bot.runner.perception import detect_screen_state

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        state = detect_screen_state(ctx)
        ctx.info("reconnect: waiting... state=%s", state.value)
        if state == InstanceState.MAIN_UI:
            ctx.info("reconnect: success, back to main UI")
            return True
        if state in (InstanceState.LOGIN_SCREEN, InstanceState.OFFLINE):
            ctx.warning("reconnect: state=%s, needs human", state.value)
            return False
        if state == InstanceState.DISCONNECTED:
            # Dialog may have reappeared after animation; tap again.
            try:
                ctx.executor.tap_text(ctx.port, ["重新登录"])
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
        return InstanceDiagnosis(
            code=InstanceIssue.LOGIN_SCREEN,
            state=state,
            needs_human=True,
            message="instance is at login screen",
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

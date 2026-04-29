"""Instance-level diagnosis used by preflight and recovery.

This module deliberately stops at classification. It does not restart the
emulator or game process; callers decide whether to skip or ask for help.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from mhxy_bot.runner.models import (
    InstanceDiagnosis,
    InstanceIssue,
    InstanceState,
)

if TYPE_CHECKING:
    from mhxy_bot.runner.context import RunnerContext


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
        return InstanceDiagnosis(
            code=InstanceIssue.DISCONNECTED,
            state=state,
            needs_human=True,
            message="game is disconnected",
        )

    reported_state = InstanceState.UNKNOWN if state == InstanceState.OFFLINE else state
    return InstanceDiagnosis(
        code=InstanceIssue.UNKNOWN_OK,
        state=reported_state,
        needs_human=False,
        message=f"instance usable, state={reported_state.value}",
        details=details,
    )

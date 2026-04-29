"""任务事件写入：有 observer 时写结构化事件，否则只写 logging。"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mhxy_bot.runner.context import RunnerContext
    from mhxy_bot.runner.models import TaskStep


def _emit(ctx: "RunnerContext", event_type: str, payload: dict[str, Any]) -> None:
    """向 observer 写入一条 runner 事件；无 observer 时写 INFO log。"""
    if ctx.observer is not None:
        try:
            ctx.observer.write_raw_event({
                "type": event_type,
                "timestamp": _now_iso(),
                **payload,
            })
        except Exception as exc:
            ctx.warning("event write failed (%s): %s", event_type, exc)
    else:
        ctx.info("[event] %s %s", event_type, payload)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def task_started(ctx: "RunnerContext", task_id: str, task_name: str) -> None:
    _emit(ctx, "task_started", {"task_id": task_id, "task_name": task_name, "port": ctx.port})


def task_completed(ctx: "RunnerContext", task_id: str, elapsed_ms: float) -> None:
    _emit(ctx, "task_completed", {"task_id": task_id, "elapsed_ms": elapsed_ms, "port": ctx.port})


def task_failed(ctx: "RunnerContext", task_id: str, step_id: str | None,
                message: str, elapsed_ms: float) -> None:
    _emit(ctx, "task_failed", {
        "task_id": task_id, "failed_step": step_id,
        "message": message, "elapsed_ms": elapsed_ms, "port": ctx.port,
    })


def task_needs_human(ctx: "RunnerContext", task_id: str, reason: str) -> None:
    _emit(ctx, "task_needs_human", {"task_id": task_id, "reason": reason, "port": ctx.port})


def step_started(ctx: "RunnerContext", task_id: str, step: "TaskStep") -> None:
    _emit(ctx, "task_step_started", {
        "task_id": task_id, "step_id": step.id,
        "action": step.action, "port": ctx.port,
    })


def step_completed(ctx: "RunnerContext", task_id: str, step: "TaskStep",
                   elapsed_ms: float, detail: dict[str, Any] | None = None) -> None:
    _emit(ctx, "task_step_completed", {
        "task_id": task_id, "step_id": step.id,
        "action": step.action, "elapsed_ms": elapsed_ms,
        "port": ctx.port, **(detail or {}),
    })


def step_failed(ctx: "RunnerContext", task_id: str, step: "TaskStep",
                message: str, attempt: int) -> None:
    _emit(ctx, "task_step_failed", {
        "task_id": task_id, "step_id": step.id,
        "action": step.action, "message": message,
        "attempt": attempt, "port": ctx.port,
    })


def denylist_triggered(ctx: "RunnerContext", task_id: str, step_id: str,
                        matched: list[str]) -> None:
    _emit(ctx, "task_denylist_triggered", {
        "task_id": task_id, "step_id": step_id,
        "matched": matched, "port": ctx.port,
    })

"""任务事件写入：有 observer 时写结构化事件，否则只写 logging。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mhxy_bot.runner.context import RunnerContext
    from mhxy_bot.runner.models import StepResult, TaskStep


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

def _step_target(step: "TaskStep") -> dict[str, Any]:
    """Extract the human-meaningful target from a step without dumping noise."""
    target: dict[str, Any] = {}
    if step.element:
        target["element"] = step.element
    if step.text:
        target["text"] = step.text
    if step.text_any:
        target["text_any"] = step.text_any
    if step.verify_text_any:
        target["verify_text_any"] = step.verify_text_any
    if step.verify_not_text_any:
        target["verify_not_text_any"] = step.verify_not_text_any
    return target


def _step_result_payload(results: list["StepResult"] | None) -> list[dict[str, Any]]:
    if not results:
        return []
    return [
        {
            "step_id": r.step_id,
            "status": getattr(r.status, "value", str(r.status)),
            "message": r.message,
            "details": r.details,
        }
        for r in results
    ]


def task_started(
    ctx: "RunnerContext",
    task_id: str,
    task_name: str,
    *,
    task_run_id: str,
    total_steps: int,
    preflight_steps: int,
    description: str = "",
) -> None:
    _emit(ctx, "task_started", {
        "task_run_id": task_run_id,
        "trace_id": task_run_id,
        "task_id": task_id,
        "task_name": task_name,
        "description": description,
        "total_steps": total_steps,
        "preflight_steps": preflight_steps,
        "main_steps": max(0, total_steps - preflight_steps),
        "port": ctx.port,
    })


def task_completed(
    ctx: "RunnerContext",
    task_id: str,
    elapsed_ms: float,
    *,
    task_run_id: str,
    step_results: list["StepResult"] | None = None,
) -> None:
    _emit(ctx, "task_completed", {
        "task_run_id": task_run_id,
        "trace_id": task_run_id,
        "task_id": task_id,
        "elapsed_ms": elapsed_ms,
        "port": ctx.port,
        "step_results": _step_result_payload(step_results),
    })


def task_failed(ctx: "RunnerContext", task_id: str, step_id: str | None,
                message: str, elapsed_ms: float, *, task_run_id: str,
                step_results: list["StepResult"] | None = None) -> None:
    _emit(ctx, "task_failed", {
        "task_run_id": task_run_id,
        "trace_id": task_run_id,
        "task_id": task_id, "failed_step": step_id,
        "message": message, "elapsed_ms": elapsed_ms, "port": ctx.port,
        "step_results": _step_result_payload(step_results),
    })


def task_needs_human(ctx: "RunnerContext", task_id: str, reason: str, *,
                     task_run_id: str, failed_step: str | None,
                     elapsed_ms: float,
                     step_results: list["StepResult"] | None = None) -> None:
    _emit(ctx, "task_needs_human", {
        "task_run_id": task_run_id,
        "trace_id": task_run_id,
        "task_id": task_id,
        "failed_step": failed_step,
        "reason": reason,
        "elapsed_ms": elapsed_ms,
        "port": ctx.port,
        "step_results": _step_result_payload(step_results),
    })


def step_started(ctx: "RunnerContext", task_id: str, step: "TaskStep", *,
                 task_run_id: str, phase: str, attempt: int,
                 max_attempts: int) -> None:
    _emit(ctx, "task_step_started", {
        "task_run_id": task_run_id,
        "trace_id": task_run_id,
        "task_id": task_id, "step_id": step.id,
        "phase": phase,
        "action": step.action,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "timeout_sec": step.timeout_sec,
        "retries": step.retries,
        "target": _step_target(step),
        "extra": step.extra,
        "port": ctx.port,
    })


def step_completed(ctx: "RunnerContext", task_id: str, step: "TaskStep",
                   elapsed_ms: float, detail: dict[str, Any] | None = None,
                   *, task_run_id: str, phase: str, attempt: int,
                   max_attempts: int, verify_ms: float | None = None) -> None:
    _emit(ctx, "task_step_completed", {
        "task_run_id": task_run_id,
        "trace_id": task_run_id,
        "task_id": task_id, "step_id": step.id,
        "phase": phase,
        "action": step.action,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "elapsed_ms": elapsed_ms,
        "duration_ms": elapsed_ms,
        "verify_ms": verify_ms,
        "target": _step_target(step),
        "detail": detail or {},
        "port": ctx.port,
    })


def step_failed(ctx: "RunnerContext", task_id: str, step: "TaskStep",
                message: str, attempt: int, *, task_run_id: str,
                phase: str, max_attempts: int, elapsed_ms: float,
                will_retry: bool, error_details: dict[str, Any] | None = None) -> None:
    _emit(ctx, "task_step_failed", {
        "task_run_id": task_run_id,
        "trace_id": task_run_id,
        "task_id": task_id, "step_id": step.id,
        "action": step.action, "message": message,
        "phase": phase,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "elapsed_ms": elapsed_ms,
        "duration_ms": elapsed_ms,
        "will_retry": will_retry,
        "target": _step_target(step),
        "error_details": error_details or {},
        "port": ctx.port,
    })


def denylist_triggered(ctx: "RunnerContext", task_id: str, step_id: str,
                        matched: list[str], *, task_run_id: str) -> None:
    _emit(ctx, "task_denylist_triggered", {
        "task_run_id": task_run_id,
        "trace_id": task_run_id,
        "task_id": task_id, "step_id": step_id,
        "matched": matched, "port": ctx.port,
    })


def scan_started(ctx: "RunnerContext", operation: str, ports: list[str]) -> None:
    """Marks the beginning of a diagnostic scan operation (trace boundary)."""
    _emit(ctx, "scan_started", {
        "operation": operation, "ports": ports, "port": ctx.port,
    })


def instance_status_port(ctx: "RunnerContext", state: str,
                         texts: list[str] | None = None) -> None:
    _emit(ctx, "instance_status", {
        "state": state, "port": ctx.port,
        "ocr_texts": texts or [],
    })


def reconnect_port(ctx: "RunnerContext", initial_state: str,
                   success: bool | None, final_state: str) -> None:
    """success=None means skipped (port was not disconnected)."""
    _emit(ctx, "reconnect_result", {
        "initial_state": initial_state,
        "success": success,
        "final_state": final_state,
        "port": ctx.port,
    })

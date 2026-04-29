"""TaskEngine：加载任务定义，逐步执行，处理 denylist、重试和 recovery。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from mhxy_bot.runner import events
from mhxy_bot.runner.actions import ActionError, execute
from mhxy_bot.runner.models import (
    StepResult,
    StepStatus,
    TaskDefinition,
    TaskResult,
    TaskStatus,
)
from mhxy_bot.runner.perception import detect_denylisted_screen
from mhxy_bot.runner import recovery as rec

if TYPE_CHECKING:
    from mhxy_bot.runner.context import RunnerContext
    from mhxy_bot.runner.models import TaskStep


class TaskEngine:
    """确定性任务执行引擎。

    使用方式：
        engine = TaskEngine(ctx)
        result = engine.run(task_def)
    或：
        result = engine.run_file("path/to/task.json")
    """

    def __init__(self, ctx: "RunnerContext") -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def run(self, task: TaskDefinition) -> TaskResult:
        """执行完整任务，返回 TaskResult。"""
        ctx = self.ctx
        t0 = time.monotonic()
        step_results: list[StepResult] = []

        ctx.info("task started: %s (%s) port=%s", task.id, task.name, ctx.port)
        events.task_started(ctx, task.id, task.name)

        for phase, steps in (("preflight", task.preflight), ("steps", task.steps)):
            for step in steps:
                result = self._run_task_step(task, step, phase, step_results, t0)
                if result is not None:
                    return result

        elapsed = _elapsed_ms(t0)
        ctx.info("task completed: %s in %.0fms", task.id, elapsed)
        events.task_completed(ctx, task.id, elapsed)
        return TaskResult(
            task_id=task.id,
            status=TaskStatus.COMPLETED,
            step_results=step_results,
        )

    def _run_task_step(
        self,
        task: TaskDefinition,
        step: "TaskStep",
        phase: str,
        step_results: list[StepResult],
        task_started_at: float,
    ) -> TaskResult | None:
        """Run one preflight/main step; return TaskResult when task must stop."""
        ctx = self.ctx

        if phase == "preflight":
            ctx.info("preflight step [%s] action=%s", step.id, step.action)

        if ctx.stop_requested:
            ctx.info("stop_requested, halting task %s at %s step %s", task.id, phase, step.id)
            result = TaskResult(
                task_id=task.id,
                status=TaskStatus.STOPPED,
                failed_step=step.id,
                message="stop_requested",
                step_results=step_results,
            )
            events.task_failed(ctx, task.id, step.id, "stopped", _elapsed_ms(task_started_at))
            return result

        sr = self._run_step(task.id, step)
        sr.details.setdefault("phase", phase)
        step_results.append(sr)

        if sr.status == StepStatus.FAILED:
            msg = sr.message
            elapsed = _elapsed_ms(task_started_at)
            ctx.error("task FAILED at %s step '%s': %s", phase, step.id, msg)
            events.task_failed(ctx, task.id, step.id, msg, elapsed)
            return TaskResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                failed_step=step.id,
                message=msg,
                step_results=step_results,
            )

        if sr.status == StepStatus.SKIPPED and sr.details.get("needs_human"):
            msg = sr.message
            elapsed = _elapsed_ms(task_started_at)
            ctx.error("task NEEDS_HUMAN at %s step '%s': %s", phase, step.id, msg)
            events.task_needs_human(ctx, task.id, msg)
            return TaskResult(
                task_id=task.id,
                status=TaskStatus.NEEDS_HUMAN,
                failed_step=step.id,
                message=msg,
                step_results=step_results,
            )

        return None

    def run_file(self, path: str | Path) -> TaskResult:
        return self.run(TaskDefinition.load(path))

    # ------------------------------------------------------------------
    # 内部：单步执行
    # ------------------------------------------------------------------

    def _run_step(self, task_id: str, step: "TaskStep") -> StepResult:
        ctx = self.ctx
        ctx.info("step [%s] action=%s", step.id, step.action)
        events.step_started(ctx, task_id, step)

        # denylist 检查
        denied = detect_denylisted_screen(ctx)
        if denied:
            msg = f"denylist triggered: {denied}"
            ctx.warning("step [%s] %s", step.id, msg)
            events.denylist_triggered(ctx, task_id, step.id, denied)
            return StepResult(
                step_id=step.id,
                status=StepStatus.SKIPPED,
                message=msg,
                details={"needs_human": True, "denylist": denied},
            )

        # 执行 + 重试
        max_attempts = 1 + max(0, step.retries)
        last_error = ""
        last_error_details: dict = {}
        for attempt in range(1, max_attempts + 1):
            t0 = time.monotonic()
            try:
                detail = execute(ctx, step)
            except ActionError as exc:
                last_error = str(exc)
                last_error_details = getattr(exc, "details", {}) or {}
                ctx.warning("step [%s] attempt %d/%d failed: %s",
                            step.id, attempt, max_attempts, last_error)
                events.step_failed(ctx, task_id, step, last_error, attempt)

                if attempt < max_attempts:
                    can_retry = rec.attempt(ctx, step.id, attempt)
                    if not can_retry:
                        return StepResult(
                            step_id=step.id,
                            status=StepStatus.SKIPPED,
                            message=f"recovery gave up: {last_error}",
                            details={"needs_human": True, **last_error_details},
                        )
                continue

            # 执行成功 — 验证后置条件
            elapsed = _elapsed_ms(t0)
            verify_err = self._verify(step)
            if verify_err:
                last_error = verify_err
                ctx.warning("step [%s] verify failed attempt %d/%d: %s",
                            step.id, attempt, max_attempts, last_error)
                events.step_failed(ctx, task_id, step, last_error, attempt)
                if attempt < max_attempts:
                    rec.attempt(ctx, step.id, attempt)
                continue

            # 全部通过
            events.step_completed(ctx, task_id, step, elapsed, detail)
            return StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                details=detail or {},
            )

        # 超过最大重试次数 → needs_human
        msg = f"step '{step.id}' failed after {max_attempts} attempts: {last_error}"
        ctx.error(msg)
        return StepResult(
            step_id=step.id,
            status=StepStatus.SKIPPED,
            message=msg,
            details={"needs_human": True, **last_error_details},
        )

    def _verify(self, step: "TaskStep") -> str:
        """验证后置条件。返回空串表示通过，否则返回错误描述。"""
        ctx = self.ctx

        if ctx.dry_run:
            return ""

        if step.verify_text_any:
            from mhxy_bot.runner.perception import has_text
            if not has_text(ctx, step.verify_text_any):
                return f"verify_text_any {step.verify_text_any} 未出现"

        if step.verify_not_text_any:
            from mhxy_bot.runner.perception import has_text
            if has_text(ctx, step.verify_not_text_any):
                return f"verify_not_text_any {step.verify_not_text_any} 仍然存在"

        return ""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _elapsed_ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 1)

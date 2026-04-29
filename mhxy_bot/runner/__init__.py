"""
mhxy_bot.runner — 确定性任务执行框架

不依赖 LangChain Agent，直接通过 ExecutorClient 驱动模拟器，
按 JSON 任务定义逐步执行，失败时自动 recovery，超限返回 needs_human。
"""
from mhxy_bot.runner.models import (
    InstanceState,
    TaskStatus,
    StepStatus,
    TaskStep,
    TaskDefinition,
    TaskResult,
    StepResult,
)
from mhxy_bot.runner.context import RunnerContext
from mhxy_bot.runner.engine import TaskEngine

__all__ = [
    "InstanceState",
    "TaskStatus",
    "StepStatus",
    "TaskStep",
    "TaskDefinition",
    "TaskResult",
    "StepResult",
    "RunnerContext",
    "TaskEngine",
]

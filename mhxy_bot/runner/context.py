"""RunnerContext：执行器引用、实例配置、全局开关。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from mhxy_bot.tools.executor_client import ExecutorClient


@dataclass
class RunnerContext:
    """执行一次任务所需的全部上下文。

    参数：
        executor:       Windows 执行器 HTTP 客户端
        port:           目标模拟器端口（字符串，如 "5557"）
        observer:       可选的 OmniObserver，用于写 task event；为 None 时只写 logging
        logger:         日志实例，默认使用模块级 logger
        dry_run:        True 时跳过所有 ADB 操作，只打印日志
        stop_requested: 外部可将此标志设为 True 来中断正在执行的任务
        extra:          任务元数据透传字段
    """
    executor: ExecutorClient
    port: str
    observer: Any | None = None
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("mhxy_bot.runner")
    )
    dry_run: bool = False
    stop_requested: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def log(self, level: int, msg: str, *args: Any) -> None:
        self.logger.log(level, msg, *args)

    def info(self, msg: str, *args: Any) -> None:
        self.logger.info(msg, *args)

    def warning(self, msg: str, *args: Any) -> None:
        self.logger.warning(msg, *args)

    def error(self, msg: str, *args: Any) -> None:
        self.logger.error(msg, *args)

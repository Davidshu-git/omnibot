"""TaskLoader：加载实例配置，构建 RunnerContext。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mhxy_bot.runner.context import RunnerContext
from mhxy_bot.tools.executor_client import ExecutorClient

_INSTANCES_PATH = Path(__file__).parent.parent.parent / "data" / "mhxy" / "config" / "instances.json"
_TASKS_DIR = Path(__file__).parent.parent / "tasks"


def load_instances(path: str | Path | None = None) -> dict:
    """加载 instances.json，返回原始 dict。"""
    p = Path(path) if path else _INSTANCES_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def get_group_ports(group_index: int, path: str | Path | None = None) -> list[str]:
    """返回指定 group 的所有端口字符串（leader 优先，然后 members）。"""
    data = load_instances(path)
    groups = data.get("groups", [])
    if group_index < 0 or group_index >= len(groups):
        raise ValueError(f"group {group_index} 不存在（共 {len(groups)} 个 group）")
    grp = groups[group_index]
    return [str(grp["leader"]["port"])] + [str(m["port"]) for m in grp.get("members", [])]


def get_all_ports(path: str | Path | None = None) -> list[str]:
    """返回所有实例端口字符串。"""
    data = load_instances(path)
    return [str(inst["port"]) for inst in data.get("instances", [])]


def make_executor(url: str | None = None, timeout: int = 30) -> ExecutorClient:
    """从参数或 MHXY_EXECUTOR_URL 环境变量创建 ExecutorClient。"""
    base_url = url or os.getenv("MHXY_EXECUTOR_URL", "http://192.168.100.149:8765")
    return ExecutorClient(base_url, timeout=timeout)


def build_context(
    port: str | int,
    executor: ExecutorClient,
    *,
    dry_run: bool = False,
    observer=None,
    extra: dict | None = None,
) -> RunnerContext:
    return RunnerContext(
        executor=executor,
        port=str(port),
        observer=observer,
        dry_run=dry_run,
        extra=extra or {},
    )

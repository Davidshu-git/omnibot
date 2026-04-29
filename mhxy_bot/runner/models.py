"""数据模型：TaskStep、TaskDefinition、TaskResult、StepResult 及状态枚举。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class InstanceState(str, Enum):
    UNKNOWN = "unknown"
    OFFLINE = "offline"
    LOGIN_SCREEN = "login_screen"
    DISCONNECTED = "disconnected"
    UPDATE_RESTART = "update_restart"
    ANDROID_HOME = "android_home"
    APP_LOADING = "app_loading"
    ACTIVITY_POPUP = "activity_popup"
    MAIN_UI = "main_ui"
    IN_TEAM = "in_team"
    IN_BATTLE = "in_battle"
    POPUP = "popup"
    STUCK = "stuck"


class InstanceIssue(str, Enum):
    ADB_OFFLINE = "adb_offline"
    SCREENSHOT_FAILED = "screenshot_failed"
    OCR_FAILED = "ocr_failed"
    LOGIN_SCREEN = "login_screen"
    DISCONNECTED = "disconnected"
    UNKNOWN_OK = "unknown_ok"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"
    STOPPED = "stopped"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskStep:
    id: str
    action: str
    element: str | None = None
    text: str | None = None
    text_any: list[str] = field(default_factory=list)
    verify_text_any: list[str] = field(default_factory=list)
    verify_not_text_any: list[str] = field(default_factory=list)
    timeout_sec: int = 30
    retries: int = 2
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskStep":
        known = {
            "id", "action", "element", "text", "text_any",
            "verify_text_any", "verify_not_text_any", "timeout_sec", "retries",
        }
        extra = {k: v for k, v in d.items() if k not in known}
        return cls(
            id=d["id"],
            action=d["action"],
            element=d.get("element"),
            text=d.get("text"),
            text_any=d.get("text_any") or [],
            verify_text_any=d.get("verify_text_any") or [],
            verify_not_text_any=d.get("verify_not_text_any") or [],
            timeout_sec=int(d.get("timeout_sec", 30)),
            retries=int(d.get("retries", 2)),
            extra=extra,
        )


@dataclass
class TaskDefinition:
    id: str
    name: str
    steps: list[TaskStep]
    preflight: list[TaskStep] = field(default_factory=list)
    description: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskDefinition":
        preflight = [TaskStep.from_dict(s) for s in d.get("preflight", [])]
        steps = [TaskStep.from_dict(s) for s in d.get("steps", [])]
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            description=d.get("description", ""),
            preflight=preflight,
            steps=steps,
            meta={k: v for k, v in d.items() if k not in {"id", "name", "description", "preflight", "steps"}},
        )

    @classmethod
    def load(cls, path: str | Path) -> "TaskDefinition":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)


@dataclass
class StepResult:
    step_id: str
    status: StepStatus
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstanceDiagnosis:
    code: InstanceIssue
    state: InstanceState
    needs_human: bool = False
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "state": self.state.value,
            "needs_human": self.needs_human,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    failed_step: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    step_results: list[StepResult] = field(default_factory=list)

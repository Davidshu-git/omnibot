"""Windows executor watchdog.

Runs on the NAS side. It checks the Windows FastAPI executor, restarts it over
SSH after consecutive failures, and writes a compact status JSON for the
observability dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import base64
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import requests


LOG_DIR = Path(os.getenv("MHXY_EXECUTOR_WATCHDOG_LOG_DIR", "/app/logs"))
STATUS_FILE = Path(os.getenv("MHXY_EXECUTOR_STATUS_FILE", str(LOG_DIR / "executor_status.json")))
EVENT_LOG = Path(os.getenv("MHXY_EXECUTOR_WATCHDOG_EVENT_LOG", str(LOG_DIR / "executor_watchdog.jsonl")))

EXECUTOR_URL = os.getenv("MHXY_EXECUTOR_URL", "http://192.168.100.149:8765").rstrip("/")
INTERVAL_SEC = int(os.getenv("MHXY_EXECUTOR_WATCHDOG_INTERVAL_SEC", "60"))
HTTP_TIMEOUT_SEC = int(os.getenv("MHXY_EXECUTOR_WATCHDOG_HTTP_TIMEOUT_SEC", "8"))
FAIL_THRESHOLD = int(os.getenv("MHXY_EXECUTOR_WATCHDOG_FAIL_THRESHOLD", "3"))
APP_HEALTH_EVERY = int(os.getenv("MHXY_EXECUTOR_WATCHDOG_APP_HEALTH_EVERY", "5"))

REMOTE_HOST = os.getenv("MHXY_REMOTE_HOST", "192.168.100.149")
REMOTE_USER = os.getenv("MHXY_REMOTE_USER", "sdw")
SSH_KEY = os.getenv("MHXY_REMOTE_SSH_KEY", "/root/.ssh_runtime/id_towin")
SSH_TARGET = f"{REMOTE_USER}@{REMOTE_HOST}"

WINDOWS_TASK_NAME = os.getenv("MHXY_EXECUTOR_TASK_NAME", "MHXYExecutorManual")
WINDOWS_EXECUTOR_DIR = os.getenv("MHXY_WINDOWS_EXECUTOR_DIR", r"C:\Users\sdw\mhxy_executor")
WINDOWS_PYTHON = os.getenv(
    "MHXY_WINDOWS_PYTHON",
    r"C:\Users\sdw\AppData\Local\Programs\Python\Python311\python.exe",
)
WINDOWS_ADB_PATH = os.getenv(
    "MHXY_WINDOWS_ADB_PATH",
    r"C:\Users\sdw\WorkBuddy\Claw\.workbuddy\mhxy\adb\platform-tools\adb.exe",
)
WINDOWS_EXECUTOR_PORT = int(os.getenv("MHXY_WINDOWS_EXECUTOR_PORT", "8765"))

TG_TOKEN = os.getenv("MHXY_TG_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN", "")
TG_USERS = os.getenv("MHXY_ALLOWED_TG_USERS") or os.getenv("ALLOWED_TG_USERS", "")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("executor_watchdog")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)


def load_ports() -> list[str]:
    raw = os.getenv("MHXY_EXECUTOR_HEALTH_PORTS", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]

    path = Path(os.getenv("MHXY_INSTANCES_PATH", "/app/data/mhxy/config/instances.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ports = [str(inst["port"]) for inst in data.get("instances", []) if inst.get("port")]
        if ports:
            return ports
    except Exception as exc:
        log.warning("load ports failed path=%s error=%s", path, exc)
    return ["5557"]


def write_status(status: dict[str, Any]) -> None:
    ensure_dirs()
    tmp = STATUS_FILE.with_suffix(STATUS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_FILE)


def append_event(event: dict[str, Any]) -> None:
    ensure_dirs()
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def notify(message: str) -> None:
    if not TG_TOKEN or not TG_USERS:
        return
    for user in [u.strip() for u in TG_USERS.split(",") if u.strip()]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": user, "text": message},
                timeout=8,
            )
        except Exception as exc:
            log.warning("telegram notify failed user=%s error=%s", user, exc)


def http_get_health() -> tuple[bool, dict[str, Any]]:
    started = time.monotonic()
    try:
        resp = requests.get(f"{EXECUTOR_URL}/health", timeout=HTTP_TIMEOUT_SEC)
        elapsed = time.monotonic() - started
        return resp.ok, {
            "ok": resp.ok,
            "status_code": resp.status_code,
            "latency_ms": round(elapsed * 1000),
            "body": resp.text[:300],
        }
    except Exception as exc:
        elapsed = time.monotonic() - started
        return False, {
            "ok": False,
            "latency_ms": round(elapsed * 1000),
            "error": repr(exc),
        }


def http_app_health(port: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        resp = requests.post(
            f"{EXECUTOR_URL}/app_health",
            json={"port": port},
            timeout=max(HTTP_TIMEOUT_SEC, 20),
        )
        elapsed = time.monotonic() - started
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return {
            "port": port,
            "ok": resp.ok,
            "status_code": resp.status_code,
            "latency_ms": round(elapsed * 1000),
            **data,
        }
    except Exception as exc:
        elapsed = time.monotonic() - started
        return {
            "port": port,
            "ok": False,
            "latency_ms": round(elapsed * 1000),
            "error": repr(exc),
        }


def ssh_run(remote_cmd: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "ssh",
            "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            SSH_TARGET,
            remote_cmd,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def powershell_encoded(script: str) -> str:
    wrapped = "$ProgressPreference='SilentlyContinue';\n" + script
    encoded = base64.b64encode(wrapped.encode("utf-16le")).decode("ascii")
    return f"powershell -NoProfile -EncodedCommand {encoded}"


def get_remote_process() -> dict[str, Any]:
    ps = powershell_encoded(
        f"""
        Get-CimInstance Win32_Process -Filter "name='python.exe'" |
          Where-Object {{ $_.CommandLine -like '*uvicorn*' -and $_.CommandLine -like '*--port*{WINDOWS_EXECUTOR_PORT}*' }} |
          Select-Object ProcessId,CreationDate,WorkingSetSize,CommandLine |
          ConvertTo-Json -Compress
        """
    )
    try:
        result = ssh_run(ps, timeout=12)
        if result.returncode != 0 or not result.stdout.strip():
            return {"ok": False, "error": (result.stderr or result.stdout).strip()[:500]}
        data = json.loads(result.stdout.strip())
        if isinstance(data, list):
            data = data[0] if data else {}
        return {
            "ok": bool(data),
            "pid": data.get("ProcessId"),
            "started_at": data.get("CreationDate"),
            "working_set_bytes": data.get("WorkingSetSize"),
            "command_line": data.get("CommandLine"),
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def restart_executor(reason: str) -> dict[str, Any]:
    task_cmd = (
        f"cmd /c cd /d {WINDOWS_EXECUTOR_DIR} "
        f"&& set ADB_PATH={WINDOWS_ADB_PATH} "
        f"&& {WINDOWS_PYTHON} -m uvicorn main:app --host 0.0.0.0 --port {WINDOWS_EXECUTOR_PORT}"
    )
    remote_cmd = powershell_encoded(
        f"""
        Get-CimInstance Win32_Process -Filter "name='python.exe'" |
          Where-Object {{ $_.CommandLine -like '*uvicorn*' -and $_.CommandLine -like '*--port*{WINDOWS_EXECUTOR_PORT}*' }} |
          ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
        Start-Sleep -Seconds 1
        $taskCmd = '{task_cmd}'
        & schtasks.exe /Create /TN '{WINDOWS_TASK_NAME}' /SC ONCE /ST 23:59 /TR $taskCmd /F
        if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
        & schtasks.exe /Run /TN '{WINDOWS_TASK_NAME}'
        exit $LASTEXITCODE
        """
    )
    started = time.monotonic()
    result = ssh_run(remote_cmd, timeout=30)
    elapsed = time.monotonic() - started
    payload = {
        "ok": result.returncode == 0,
        "reason": reason,
        "latency_ms": round(elapsed * 1000),
        "returncode": result.returncode,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }
    append_event({"timestamp": utc_now(), "event": "restart", **payload})
    if payload["ok"]:
        notify(f"MHXY Windows executor 已自动重启\n原因：{reason}")
    else:
        notify(f"MHXY Windows executor 自动重启失败\n原因：{reason}\n错误：{payload['stderr'] or payload['stdout']}")
    return payload


def run_once(iteration: int, consecutive_failures: int, ports: list[str]) -> tuple[int, dict[str, Any]]:
    checked_at = utc_now()
    health_ok, health = http_get_health()
    app_results: list[dict[str, Any]] = []
    if health_ok and APP_HEALTH_EVERY > 0 and (iteration == 1 or iteration % APP_HEALTH_EVERY == 0):
        app_results = [http_app_health(port) for port in ports]
        health_ok = all(r.get("healthy") is True for r in app_results)

    consecutive_failures = 0 if health_ok else consecutive_failures + 1
    process = get_remote_process()
    restart: dict[str, Any] | None = None
    if consecutive_failures >= FAIL_THRESHOLD:
        reason = f"health failed {consecutive_failures} consecutive checks"
        log.warning("restart triggered: %s", reason)
        restart = restart_executor(reason)
        consecutive_failures = 0
        time.sleep(5)
        health_ok, health = http_get_health()
        process = get_remote_process()

    status = {
        "service": "mhxy_windows_executor",
        "executor_url": EXECUTOR_URL,
        "status": "healthy" if health_ok else "unhealthy",
        "checked_at": checked_at,
        "consecutive_failures": consecutive_failures,
        "fail_threshold": FAIL_THRESHOLD,
        "interval_sec": INTERVAL_SEC,
        "health": health,
        "app_health": app_results,
        "process": process,
        "last_restart": restart,
    }
    write_status(status)
    append_event({"timestamp": checked_at, "event": "check", "status": status["status"], "health": health, "process": process})
    return consecutive_failures, status


def main() -> None:
    ensure_dirs()
    ports = load_ports()
    log.info(
        "executor watchdog started url=%s target=%s ports=%s interval=%ss threshold=%s",
        EXECUTOR_URL, SSH_TARGET, ",".join(ports), INTERVAL_SEC, FAIL_THRESHOLD,
    )
    consecutive_failures = 0
    iteration = 0
    while True:
        iteration += 1
        try:
            consecutive_failures, status = run_once(iteration, consecutive_failures, ports)
            log.info(
                "check status=%s failures=%s latency=%sms pid=%s",
                status["status"],
                consecutive_failures,
                status["health"].get("latency_ms"),
                status["process"].get("pid"),
            )
        except Exception:
            log.exception("watchdog loop failed")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()

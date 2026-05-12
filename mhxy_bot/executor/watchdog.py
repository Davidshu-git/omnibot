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
from email.utils import parsedate_to_datetime
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
SYNC_EVENTS_EVERY = int(os.getenv("MHXY_EXECUTOR_EVENTS_SYNC_EVERY", "1"))
SYNC_FAIL_NOTIFY_THRESHOLD = int(os.getenv("MHXY_EXECUTOR_EVENTS_SYNC_FAIL_NOTIFY_THRESHOLD", "5"))

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
    r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
)
WINDOWS_EXECUTOR_PORT = int(os.getenv("MHXY_WINDOWS_EXECUTOR_PORT", "8765"))
EXECUTOR_EVENTS_REMOTE_DIR = os.getenv(
    "MHXY_EXECUTOR_EVENTS_REMOTE_DIR",
    r"C:/Users/sdw/mhxy_executor/events",
)
EXECUTOR_EVENTS_LOCAL_DIR = Path(os.getenv(
    "MHXY_EXECUTOR_EVENTS_LOCAL_DIR",
    "/logs/omnibot/mhxy/executor",
))
OBS_API_URL = os.getenv("MHXY_OBS_API_URL", "http://obs-api:8000").rstrip("/")

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


def read_status() -> dict[str, Any]:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("read previous status failed path=%s error=%s", STATUS_FILE, exc)
        return {}


def append_event(event: dict[str, Any]) -> None:
    ensure_dirs()
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _decode_remote_json(stdout: str) -> list[dict[str, Any]]:
    """解析 PowerShell ConvertTo-Json 输出，兼容单文件对象和空输出。"""
    text = stdout.strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _remote_event_listing() -> list[dict[str, Any]]:
    """列出远端当日/昨日 executor event 文件元数据。"""
    remote_dir = EXECUTOR_EVENTS_REMOTE_DIR.replace("/", "\\")
    ps = powershell_encoded(
        f"""
        $dir = '{remote_dir}'
        if (-not (Test-Path $dir)) {{ return }}
        Get-ChildItem -Path $dir -Filter 'executor_events_*.jsonl' |
          Select-Object Name,Length,@{{n='Mtime';e={{$_.LastWriteTimeUtc.ToString('o')}}}} |
          ConvertTo-Json -Compress
        """
    )
    result = ssh_run(ps, timeout=20)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:500])
    return _decode_remote_json(result.stdout)


def _parse_remote_mtime(value: Any) -> float | None:
    """把 PowerShell ISO 时间转为 epoch 秒。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError):
            return None


def sync_executor_events() -> dict[str, Any]:
    """从 Windows executor 增量拉取结构化事件 JSONL。"""
    EXECUTOR_EVENTS_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    remote_files = _remote_event_listing()
    to_pull: list[str] = []
    skipped: list[str] = []
    for item in remote_files:
        name = str(item.get("Name") or "")
        if not name.startswith("executor_events_") or not name.endswith(".jsonl"):
            continue
        remote_size = int(item.get("Length") or 0)
        remote_mtime = _parse_remote_mtime(item.get("Mtime"))
        local = EXECUTOR_EVENTS_LOCAL_DIR / name
        if local.exists():
            stat = local.stat()
            local_mtime_ok = remote_mtime is None or stat.st_mtime >= remote_mtime - 1
            if stat.st_size == remote_size and local_mtime_ok:
                skipped.append(name)
                continue
        to_pull.append(name)

    pulled: list[str] = []
    errors: list[str] = []
    for name in to_pull:
        remote_path = f"{EXECUTOR_EVENTS_REMOTE_DIR.rstrip('/')}/{name}"
        local_path = EXECUTOR_EVENTS_LOCAL_DIR / name
        result = subprocess.run(
            [
                "scp",
                "-p",
                "-i", SSH_KEY,
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                f"{SSH_TARGET}:{remote_path}",
                str(local_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if result.returncode == 0:
            pulled.append(name)
        else:
            errors.append(f"{name}: {(result.stderr or result.stdout).strip()[:300]}")

    if errors:
        raise RuntimeError("; ".join(errors))
    return {"pulled": pulled, "skipped": skipped, "count": len(pulled)}


def notify_obs_ingest() -> None:
    """通知 obs-api 立即摄取 executor JSONL。"""
    try:
        requests.post(f"{OBS_API_URL}/api/ingest/mhxy-executor", timeout=10)
    except requests.RequestException as exc:
        log.warning("notify obs ingest failed: %s", exc)


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
    # 第 1 步：裸 taskkill 杀进程（Stop-Process 在 SSH 会话下无效，实测确认）
    kill_cmd = "taskkill /IM python.exe /F"
    # 第 2 步：生成启动脚本并用 Start-Process cmd.exe 后台拉起（schtasks 方式不可靠，实测确认）
    remote_cmd = powershell_encoded(
        f"""
        $cmdPath = '{WINDOWS_EXECUTOR_DIR}\\start_executor.cmd'
        $lines = @(
          '@echo off',
          'cd /d {WINDOWS_EXECUTOR_DIR}',
          'set ADB_PATH={WINDOWS_ADB_PATH}',
          'set EXECUTOR_EVENTS_DIR={WINDOWS_EXECUTOR_DIR}\\events',
          'if not exist "%EXECUTOR_EVENTS_DIR%" mkdir "%EXECUTOR_EVENTS_DIR%"',
          '"{WINDOWS_PYTHON}" -m uvicorn main:app --host 0.0.0.0 --port {WINDOWS_EXECUTOR_PORT} >> "{WINDOWS_EXECUTOR_DIR}\\stdout-watchdog.log" 2>> "{WINDOWS_EXECUTOR_DIR}\\stderr-watchdog.log"'
        )
        Set-Content -Path $cmdPath -Value $lines -Encoding ASCII
        Start-Process cmd.exe -ArgumentList "/c $cmdPath" -WindowStyle Hidden
        """
    )
    started = time.monotonic()
    ssh_run(kill_cmd, timeout=10)  # 杀进程，忽略返回值（进程不存在时也会报错）
    time.sleep(3)                  # 等端口释放
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
    ports = load_ports()  # 每轮重新读取，instances.json 变更后自动生效
    checked_at = utc_now()
    previous = read_status()
    health_ok, health = http_get_health()
    previous_app_results = previous.get("app_health")
    app_results: list[dict[str, Any]] = previous_app_results if isinstance(previous_app_results, list) else []
    app_health_checked_at = previous.get("app_health_checked_at")

    should_check_app_health = health_ok and APP_HEALTH_EVERY > 0 and (
        iteration == 1 or iteration % APP_HEALTH_EVERY == 0
    )
    if should_check_app_health:
        app_results = [http_app_health(port) for port in ports]
        app_health_checked_at = checked_at
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
        if health_ok and APP_HEALTH_EVERY > 0:
            app_results = [http_app_health(port) for port in ports]
            app_health_checked_at = utc_now()
            health_ok = all(r.get("healthy") is True for r in app_results)
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
        "app_health_checked_at": app_health_checked_at,
        "process": process,
        "last_restart": restart,
    }
    write_status(status)
    append_event({"timestamp": checked_at, "event": "check", "status": status["status"], "health": health, "process": process})
    return consecutive_failures, status


def main() -> None:
    ensure_dirs()
    EXECUTOR_EVENTS_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    ports = load_ports()
    log.info(
        "executor watchdog started url=%s target=%s ports=%s interval=%ss threshold=%s",
        EXECUTOR_URL, SSH_TARGET, ",".join(ports), INTERVAL_SEC, FAIL_THRESHOLD,
    )
    consecutive_failures = 0
    sync_failures = 0
    iteration = 0
    while True:
        iteration += 1
        try:
            consecutive_failures, status = run_once(iteration, consecutive_failures, ports)
            if SYNC_EVENTS_EVERY > 0 and iteration % SYNC_EVENTS_EVERY == 0:
                try:
                    sync_result = sync_executor_events()
                    sync_failures = 0
                    append_event({"timestamp": utc_now(), "event": "sync_executor_events", **sync_result})
                    if sync_result.get("count", 0) > 0:
                        notify_obs_ingest()
                except Exception as exc:
                    sync_failures += 1
                    append_event({
                        "timestamp": utc_now(),
                        "event": "sync_executor_events_failed",
                        "failures": sync_failures,
                        "error": repr(exc),
                    })
                    log.warning("sync executor events failed count=%s error=%s", sync_failures, exc)
                    if sync_failures == SYNC_FAIL_NOTIFY_THRESHOLD:
                        notify(f"MHXY executor 事件日志同步连续失败 {sync_failures} 次\n错误：{exc}")
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

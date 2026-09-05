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
# 重启后冷却期：在此窗口内健康失败不累计 consecutive_failures，避免 executor
# 启动慢（RapidOCR 加载 ONNX ~15s）导致 watchdog 再次 taskkill 形成 kill 循环。
RESTART_COOLDOWN_SEC = int(os.getenv("MHXY_EXECUTOR_WATCHDOG_RESTART_COOLDOWN_SEC", "90"))
APP_HEALTH_EVERY = int(os.getenv("MHXY_EXECUTOR_WATCHDOG_APP_HEALTH_EVERY", "5"))
# adb 守护进程自愈：executor 健康但 /list_devices count < 期望端口数 连续 N 轮 →
# 判定 adb server（:5037 那个 adb 自带的进程）设备表 stale，远程 kill-server/start-server。
# 包含两种典型场景：
#   1) count==0：MuMu 批量重启后 adb 设备表全空（执行 executor /list_devices 仍 OK）。
#   2) 0 < count < expected：部分 MuMu 实例重启时机错过 adb daemon 的 emulator
#      console 扫描，adb devices 缺失部分实例（实测中常见，详见 ops/windows_executor.md）。
# 重启 executor 对两种情况均无效——adb daemon 不归 executor 管。
ADB_FAIL_THRESHOLD = int(os.getenv("MHXY_EXECUTOR_WATCHDOG_ADB_FAIL_THRESHOLD", "2"))
# adb 重置冷却：start-server 后实例注册有时间差，count 可能短暂仍为 0/部分。
# 冷却窗口内不重复 kill-server，避免 reset 风暴（与 executor 重启冷却独立）。
ADB_RESET_COOLDOWN_SEC = int(os.getenv("MHXY_EXECUTOR_WATCHDOG_ADB_RESET_COOLDOWN_SEC", "120"))
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
POWER_FILE = Path(os.getenv(
    "MHXY_EXECUTOR_POWER_FILE",
    "/app/data/mhxy/config/executor_power.json",
))
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
    """加载需要健康检查的实例端口。

    优先取环境变量 MHXY_EXECUTOR_HEALTH_PORTS（逗号分隔），否则读 instances.json。

    Returns:
        端口字符串列表，保证非空。

    Raises:
        RuntimeError: 配置不可读、格式非法，或未解析出任何端口。此时宁可让
            watchdog 显式失败，也不能兜底猜一个端口或返回空列表——ports 为空会
            让深检的 all([]) 与 adb 判定的 devices_count >= expected_count(0)
            双双恒真，把整套健康检查静默变成「永远健康」。
    """
    raw = os.getenv("MHXY_EXECUTOR_HEALTH_PORTS", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]

    path = Path(os.getenv("MHXY_INSTANCES_PATH", "/app/data/mhxy/config/instances.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("load ports failed path=%s error=%s", path, exc)
        raise RuntimeError(f"无法读取实例配置 {path}：{exc}") from exc

    instances = data.get("instances", []) if isinstance(data, dict) else []
    ports = [str(inst["port"]) for inst in instances if isinstance(inst, dict) and inst.get("port")]
    if not ports:
        log.error("no ports parsed from instances config path=%s", path)
        raise RuntimeError(
            f"实例配置 {path} 未解析出任何端口；"
            "如需临时指定，设置环境变量 MHXY_EXECUTOR_HEALTH_PORTS=5559,5561"
        )
    return ports


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


def _power_file_mtime() -> float:
    try:
        return POWER_FILE.stat().st_mtime
    except OSError:
        return 0.0


def interruptible_wait(timeout: float, baseline_mtime: float) -> float:
    """睡 timeout 秒，但 POWER_FILE 一旦变更立即提前返回，返回退出时 mtime 作下轮基线。

    不引入第三方 inotify 库（项目规范：新增依赖需先询问），用 1s 粒度 mtime
    轮询实现等价效果——obs 侧开关后 watchdog 反应从 ≤INTERVAL_SEC 降到 ≤1s。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        m = _power_file_mtime()
        if m != baseline_mtime:
            log.info("power file changed, waking watchdog early")
            return m
    return _power_file_mtime()


def read_power_enabled() -> bool:
    """读 obs 侧写的电源开关。文件缺失/损坏一律视为启用（向后兼容）。"""
    try:
        data = json.loads(POWER_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except Exception as exc:
        log.warning("read power file failed path=%s error=%s, assume enabled", POWER_FILE, exc)
        return True
    enabled = data.get("enabled")
    return enabled if isinstance(enabled, bool) else True


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


def http_list_devices() -> tuple[int | None, dict[str, Any]]:
    """探测 executor /list_devices，返回 (count, detail)。

    用于识别 adb server 设备表 stale 的特征：模拟器批量重启后，adb daemon
    进程不退出但内存设备表清空 → executor `/health` 仍 OK，而 `/list_devices`
    count==0。仅当 HTTP 调用本身成功（detail["ok"] is True）时 count 才可信，
    调用失败返回 ``(None, {"ok": False, ...})``，调用方据此不误判 stale。

    Returns:
        tuple: ``(count, detail)``。count 为 adb 可见实例数，调用失败时为 None。
    """
    started = time.monotonic()
    try:
        resp = requests.get(f"{EXECUTOR_URL}/list_devices", timeout=HTTP_TIMEOUT_SEC)
        elapsed = time.monotonic() - started
        if not resp.ok:
            return None, {
                "ok": False,
                "status_code": resp.status_code,
                "latency_ms": round(elapsed * 1000),
                "body": resp.text[:300],
            }
        data = resp.json()
        count = int(data.get("count", 0))
        return count, {
            "ok": True,
            "status_code": resp.status_code,
            "latency_ms": round(elapsed * 1000),
            "count": count,
            "ports": data.get("ports", []),
        }
    except Exception as exc:
        elapsed = time.monotonic() - started
        return None, {
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


def stop_executor(reason: str) -> dict[str, Any]:
    """仅杀进程，不重启——供电源开关「关闭」态使用。"""
    started = time.monotonic()
    result = ssh_run("taskkill /IM python.exe /F", timeout=10)
    elapsed = time.monotonic() - started
    payload = {
        "ok": result.returncode == 0,
        "reason": reason,
        "at": utc_now(),
        "latency_ms": round(elapsed * 1000),
        "returncode": result.returncode,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }
    append_event({"timestamp": utc_now(), "event": "stop", **payload})
    notify(f"MHXY Windows executor 已按 obs 开关停用\n原因：{reason}")
    return payload


def restart_executor(reason: str) -> dict[str, Any]:
    # 第 1 步：裸 taskkill 杀进程（Stop-Process 在 SSH 会话下无效，实测确认）
    kill_cmd = "taskkill /IM python.exe /F"
    # 第 2 步：生成启动脚本并用 WMI Win32_Process.Create 拉起。
    # 关键：Windows OpenSSH 把会话内子进程放进同一 Job Object，SSH 客户端断开
    # 时整个 Job 被终止——Start-Process / VBScript 都救不了。WMI 创建的进程
    # 不继承父 Job，是目前实测唯一能脱离 SSH 会话独立存活的方式。
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
        Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine = ('cmd.exe /c ' + $cmdPath)}} | Out-Null
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
        "at": utc_now(),
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


def reset_adb_server(reason: str) -> dict[str, Any]:
    """重启 Windows 上 adb 自带的 :5037 守护进程，强制重建设备表。

    模拟器（MuMu）批量或部分重启后，adb server 进程不退出但内存 transport
    表残缺，executor `/health` 仍 OK 而 `/list_devices` count 低于期望（最差
    情况为 0）。此时重启 executor 无效（adb daemon 不归 executor 管），唯一
    修复是 ``adb kill-server && start-server`` 让其全量重新发现实例。

    复用 watchdog 已有的 SSH 通道，与 :func:`restart_executor` 一致，改动不
    扩散到 Windows 侧（无需新增 executor 端点）。

    Args:
        reason: 触发自愈的原因，写入事件日志与 Telegram 通知。

    Returns:
        dict: 含 ``ok`` / ``at`` / ``latency_ms`` / ``stdout`` / ``stderr`` 的执行回执。
    """
    ps = powershell_encoded(
        f"""
        $adb = '{WINDOWS_ADB_PATH}'
        & $adb kill-server
        Start-Sleep -Seconds 2
        & $adb start-server
        Start-Sleep -Seconds 4
        & $adb devices
        """
    )
    started = time.monotonic()
    result = ssh_run(ps, timeout=30)
    elapsed = time.monotonic() - started
    payload = {
        "ok": result.returncode == 0,
        "reason": reason,
        "at": utc_now(),
        "latency_ms": round(elapsed * 1000),
        "returncode": result.returncode,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }
    append_event({"timestamp": utc_now(), "event": "adb_reset", **payload})
    return payload


def run_once(iteration: int, consecutive_failures: int, ports: list[str]) -> tuple[int, dict[str, Any]]:
    ports = load_ports()  # 每轮重新读取，instances.json 变更后自动生效
    checked_at = utc_now()
    previous = read_status()

    # 电源开关「关闭」：跳过健康检查与自动重启，确保进程已停。
    # 仅在进程仍在跑时执行一次 stop（状态转换），后续轮次不再重复 kill/notify。
    if not read_power_enabled():
        process = get_remote_process()
        stop: dict[str, Any] | None = None
        if process.get("ok"):
            stop = stop_executor("obs 电源开关已关闭")
            process = get_remote_process()
        status = {
            "service": "mhxy_windows_executor",
            "executor_url": EXECUTOR_URL,
            "status": "disabled",
            "checked_at": checked_at,
            "consecutive_failures": 0,
            "fail_threshold": FAIL_THRESHOLD,
            "interval_sec": INTERVAL_SEC,
            "health": {"ok": False, "detail": "executor disabled via obs power switch"},
            "app_health": [],
            "app_health_checked_at": previous.get("app_health_checked_at"),
            "process": process,
            "last_stop": stop or (previous.get("last_stop") if isinstance(previous, dict) else None),
        }
        write_status(status)
        append_event({"timestamp": checked_at, "event": "check", "status": "disabled", "process": process})
        return 0, status

    health_ok, health = http_get_health()

    # 重新开启瞬间：上一轮是 disabled、本轮已 enabled，executor 仍是被 stop 的死状态。
    # 不等 FAIL_THRESHOLD 累积，直接把失败计数置满，让下方逻辑本轮立即重启，
    # 把恢复时延从 ~3-4 分钟压到 ≤60s + 启动时间。
    just_reenabled = isinstance(previous, dict) and previous.get("status") == "disabled"
    if just_reenabled and not health_ok:
        log.info("executor re-enabled via obs power switch, forcing immediate restart")
        consecutive_failures = FAIL_THRESHOLD

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

    in_cooldown = False
    cooldown_remaining = 0.0
    last_restart_at_str = (previous.get("last_restart") or {}).get("at")
    if last_restart_at_str and RESTART_COOLDOWN_SEC > 0:
        try:
            last_dt = datetime.fromisoformat(last_restart_at_str)
            elapsed_since_restart = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if elapsed_since_restart < RESTART_COOLDOWN_SEC:
                in_cooldown = True
                cooldown_remaining = RESTART_COOLDOWN_SEC - elapsed_since_restart
        except ValueError:
            pass

    if health_ok:
        consecutive_failures = 0
    elif in_cooldown:
        log.info("in restart cooldown (%.0fs left), suppress fail count", cooldown_remaining)
    else:
        consecutive_failures += 1
    process = get_remote_process()
    restart: dict[str, Any] | None = None
    if consecutive_failures >= FAIL_THRESHOLD:
        reason = (
            "re-enabled via obs power switch"
            if just_reenabled and not health_ok
            else f"health failed {consecutive_failures} consecutive checks"
        )
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

    # ── adb 守护进程自愈 ──────────────────────────────────────────────
    # executor 健康但 /list_devices count < 期望端口数 连续 ADB_FAIL_THRESHOLD
    # 轮 → 判定 adb server（adb 自带的 :5037 进程）设备表 stale（全部丢失 or
    # 部分丢失）。重启 executor 对此无效，需远程 kill/start adb server 让其
    # 全量重新发现 MuMu 实例。
    expected_count = len(ports)
    prev_adb = previous.get("adb") if isinstance(previous, dict) else {}
    if not isinstance(prev_adb, dict):
        prev_adb = {}
    # 字段 zero_streak → stale_streak 的兼容读取（旧 status 文件首次升级时回退）
    adb_stale_streak = int(
        prev_adb.get("stale_streak", prev_adb.get("zero_streak", 0)) or 0
    )
    adb_reset: dict[str, Any] | None = None

    if health_ok:
        devices_count, devices_detail = http_list_devices()
    else:
        # executor 本身不健康时由上方 restart 路径处理，不在此累计 adb streak
        devices_count, devices_detail = None, {"ok": False, "skipped": "executor unhealthy"}

    # 仅当 /list_devices 调用本身成功时 count 才可信，避免把瞬时 HTTP 抖动误判 stale
    if health_ok and devices_detail.get("ok") is True:
        is_short = devices_count is not None and devices_count < expected_count
        adb_stale_streak = adb_stale_streak + 1 if is_short else 0

    adb_in_cooldown = False
    last_adb_reset_at = (prev_adb.get("last_reset") or {}).get("at")
    if last_adb_reset_at and ADB_RESET_COOLDOWN_SEC > 0:
        try:
            elapsed_since_adb = (
                datetime.now(timezone.utc) - datetime.fromisoformat(last_adb_reset_at)
            ).total_seconds()
            adb_in_cooldown = elapsed_since_adb < ADB_RESET_COOLDOWN_SEC
        except ValueError:
            pass

    if (
        health_ok
        and adb_stale_streak >= ADB_FAIL_THRESHOLD
        and not adb_in_cooldown
        and restart is None  # 本轮没刚重启 executor，避免叠加扰动
    ):
        reason = (
            f"adb device table stale ({adb_stale_streak} checks, "
            f"count={devices_count}/{expected_count}, executor healthy)"
        )
        log.warning("adb self-heal triggered: %s", reason)
        adb_reset = reset_adb_server(reason)
        adb_stale_streak = 0
        time.sleep(4)  # start-server 后实例注册有时间差
        devices_count, devices_detail = http_list_devices()
        recovered = (
            devices_detail.get("ok") is True
            and devices_count is not None
            and devices_count >= expected_count
        )
        # adb 自愈后强制刷一次 app_health 深检，让 obs /instances 立即看到新状态，
        # 否则得等到下一次自然深检（最长 APP_HEALTH_EVERY 轮 ≈5 分钟）才能反映恢复结果。
        # APP_HEALTH_EVERY==0 时整体禁用深检，这里也不强制（尊重配置）。
        if APP_HEALTH_EVERY > 0:
            app_results = [http_app_health(port) for port in ports]
            app_health_checked_at = utc_now()
            health_ok = all(r.get("healthy") is True for r in app_results)
        healthy_after = sum(1 for r in app_results if r.get("healthy") is True)
        if recovered:
            notify(
                f"MHXY adb 守护进程已自愈\n原因：{reason}\n"
                f"恢复设备数：{devices_count}/{expected_count}\n"
                f"实例健康：{healthy_after}/{expected_count}"
            )
        else:
            notify(
                "MHXY adb 自愈后仍未完全恢复\n"
                f"原因：{reason}\ncount={devices_count}/{expected_count}\n"
                f"实例健康：{healthy_after}/{expected_count}\n"
                "（可能仍有 MuMu 实例未启动或 emulator console 未就绪，需人工检查 Windows 主机）"
            )

    adb_block = {
        "count": devices_count,
        "expected": expected_count,
        "ok": (
            devices_detail.get("ok") is True
            and devices_count is not None
            and devices_count >= expected_count
        ),
        "stale_streak": adb_stale_streak,
        "fail_threshold": ADB_FAIL_THRESHOLD,
        "in_cooldown": adb_in_cooldown,
        "detail": devices_detail,
        "last_reset": adb_reset or prev_adb.get("last_reset"),
        "checked_at": checked_at,
    }

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
        "adb": adb_block,
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
    power_mtime = _power_file_mtime()
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
                "check status=%s failures=%s latency=%sms pid=%s adb=%s/%s streak=%s",
                status["status"],
                consecutive_failures,
                status["health"].get("latency_ms"),
                status["process"].get("pid"),
                status.get("adb", {}).get("count"),
                status.get("adb", {}).get("expected"),
                status.get("adb", {}).get("stale_streak"),
            )
        except Exception:
            log.exception("watchdog loop failed")
        power_mtime = interruptible_wait(INTERVAL_SEC, power_mtime)


if __name__ == "__main__":
    main()

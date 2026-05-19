"""watchdog adb 自愈逻辑确定性验证（mock 网络/SSH，零真实环境影响）。

场景：executor /health 持续 OK，但 /list_devices count==0（adb 设备表 stale）。
断言：
  1. 未达阈值不触发 reset；
  2. 连续 ADB_FAIL_THRESHOLD 轮 count==0 → 触发 reset_adb_server 一次；
  3. reset 冷却期内即使仍 count==0 也不重复 reset（防风暴）；
  4. count 恢复 >0 后 zero_streak 清零、ok=True。
运行：docker cp 进 watchdog 容器后 python 直跑（用 /tmp 隔离 STATUS_FILE）。
"""
import os
import tempfile

os.environ["MHXY_EXECUTOR_STATUS_FILE"] = os.path.join(tempfile.gettempdir(), "wd_test_status.json")
os.environ["MHXY_EXECUTOR_WATCHDOG_LOG_DIR"] = tempfile.gettempdir()
os.environ["MHXY_EXECUTOR_WATCHDOG_EVENT_LOG"] = os.path.join(tempfile.gettempdir(), "wd_test_events.jsonl")

import importlib

wd = importlib.import_module("mhxy_bot.executor.watchdog")

# 干净起点
for p in (wd.STATUS_FILE, wd.EVENT_LOG):
    try:
        p.unlink()
    except OSError:
        pass

reset_calls: list[str] = []
notify_msgs: list[str] = []
DEVICES_COUNT = {"v": 0}  # 可在用例间切换


def fake_health():
    return True, {"ok": True, "status_code": 200, "latency_ms": 5, "body": "{}"}


def fake_list_devices():
    c = DEVICES_COUNT["v"]
    return c, {"ok": True, "status_code": 200, "latency_ms": 5, "count": c, "ports": list(range(c))}


def fake_reset(reason: str):
    reset_calls.append(reason)
    return {"ok": True, "reason": reason, "at": wd.utc_now(), "latency_ms": 10,
            "returncode": 0, "stdout": "", "stderr": ""}


wd.http_get_health = fake_health
wd.http_list_devices = fake_list_devices
wd.reset_adb_server = fake_reset
wd.get_remote_process = lambda: {"ok": True, "pid": 1}
wd.read_power_enabled = lambda: True
wd.notify = lambda m: notify_msgs.append(m)
wd.APP_HEALTH_EVERY = 0  # 关掉 app_health 深检，隔离 adb 路径
wd.ADB_RESET_COOLDOWN_SEC = 120
wd.ADB_FAIL_THRESHOLD = 2
PORTS = ["5557", "5559"]


def adb(status):
    return status["adb"]


def run(it):
    _, st = wd.run_once(it, 0, PORTS)
    return st


# ── 用例 1：第 1 轮 count=0，未达阈值(2)，不触发 ──────────────────
DEVICES_COUNT["v"] = 0
s1 = run(1)
assert adb(s1)["zero_streak"] == 1, adb(s1)
assert adb(s1)["ok"] is False
assert reset_calls == [], reset_calls
print("[1] streak=1, 未触发 reset  ✓")

# ── 用例 2：第 2 轮 count=0，达阈值 → 触发一次 reset ────────────────
s2 = run(2)
assert reset_calls and len(reset_calls) == 1, reset_calls
assert adb(s2)["zero_streak"] == 0, adb(s2)  # 触发后清零
assert adb(s2)["last_reset"] is not None
assert any("仍未恢复" in m for m in notify_msgs), notify_msgs  # reset 后仍 0 → 告警
print(f"[2] 阈值触发 reset 1 次，reason={reset_calls[0]!r}  ✓")

# ── 用例 3：冷却期内仍 count=0，不重复 reset（防风暴）──────────────
s3 = run(3)
s4 = run(4)
assert len(reset_calls) == 1, f"冷却期内不应再 reset, got {len(reset_calls)}"
assert adb(s4)["in_cooldown"] is True
print("[3] 冷却期内连续 count=0，未重复 reset  ✓")

# ── 用例 4：count 恢复 → streak 清零、ok=True ─────────────────────
DEVICES_COUNT["v"] = 7
s5 = run(5)
assert adb(s5)["count"] == 7
assert adb(s5)["zero_streak"] == 0
assert adb(s5)["ok"] is True
print("[4] count 恢复 7，streak 清零 ok=True  ✓")

print("\n全部断言通过 ✅  自愈决策路径（检测/阈值/冷却/恢复）行为正确")

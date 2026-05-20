"""watchdog adb 自愈逻辑确定性验证（mock 网络/SSH，零真实环境影响）。

场景：executor /health 持续 OK，但 /list_devices count < expected（adb 设备表 stale）。
断言：
  1. 未达阈值不触发 reset；
  2. 连续 ADB_FAIL_THRESHOLD 轮 count<expected → 触发 reset_adb_server 一次；
  3. reset 冷却期内即使仍 stale 也不重复 reset（防风暴）；
  4. count 恢复 >=expected 后 stale_streak 清零、ok=True；
  5. 部分丢失（0 < count < expected）同样累计 streak 并在阈值后触发 reset。
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
wd.load_ports = lambda: PORTS  # 容器内默认会读真实 instances.json，覆盖之


def adb(status):
    return status["adb"]


def run(it):
    _, st = wd.run_once(it, 0, PORTS)
    return st


# ── 用例 1：第 1 轮 count=0（< expected=2），未达阈值(2)，不触发 ─────────
DEVICES_COUNT["v"] = 0
s1 = run(1)
assert adb(s1)["stale_streak"] == 1, adb(s1)
assert adb(s1)["ok"] is False
assert adb(s1)["expected"] == 2, adb(s1)
assert reset_calls == [], reset_calls
print("[1] streak=1, 未触发 reset  ✓")

# ── 用例 2：第 2 轮 count=0，达阈值 → 触发一次 reset ────────────────
s2 = run(2)
assert reset_calls and len(reset_calls) == 1, reset_calls
assert adb(s2)["stale_streak"] == 0, adb(s2)  # 触发后清零
assert adb(s2)["last_reset"] is not None
assert any("仍未完全恢复" in m for m in notify_msgs), notify_msgs  # reset 后仍 stale → 告警
print(f"[2] 阈值触发 reset 1 次，reason={reset_calls[0]!r}  ✓")

# ── 用例 3：冷却期内仍 count=0，不重复 reset（防风暴）──────────────
s3 = run(3)
s4 = run(4)
assert len(reset_calls) == 1, f"冷却期内不应再 reset, got {len(reset_calls)}"
assert adb(s4)["in_cooldown"] is True
print("[3] 冷却期内连续 count=0，未重复 reset  ✓")

# ── 用例 4：count 恢复 >= expected → streak 清零、ok=True ──────────
DEVICES_COUNT["v"] = 7
s5 = run(5)
assert adb(s5)["count"] == 7
assert adb(s5)["stale_streak"] == 0
assert adb(s5)["ok"] is True
print("[4] count 恢复 7 >= expected 2，streak 清零 ok=True  ✓")

# ── 用例 5：部分丢失（0 < count < expected）也要累计并触发 reset ────
# 清掉冷却 + 重置 reset 调用计数，模拟"adb 自愈早已过去"
wd.ADB_RESET_COOLDOWN_SEC = 0
reset_calls.clear()
notify_msgs.clear()

DEVICES_COUNT["v"] = 1  # expected=2 → 部分丢失
s6 = run(6)
assert adb(s6)["stale_streak"] == 1, adb(s6)
assert adb(s6)["ok"] is False
assert reset_calls == [], "部分丢失第一次不应触发"
print("[5.1] count=1 < expected=2，streak=1，未触发  ✓")

s7 = run(7)
assert reset_calls and len(reset_calls) == 1, reset_calls
assert "count=1/2" in reset_calls[0], reset_calls[0]
print(f"[5.2] 部分丢失达阈值触发 reset 1 次，reason={reset_calls[0]!r}  ✓")

# ── 用例 6：自愈成功后强制 app_health 深检（APP_HEALTH_EVERY>0 时） ─
# 避免 obs /instances 等到下一次自然深检（最长 5 分钟）才看到恢复。
wd.APP_HEALTH_EVERY = 5  # 重新打开深检（前面置 0 隔离了 adb 路径，这里要测刷新）
wd.ADB_RESET_COOLDOWN_SEC = 0
reset_calls.clear()
notify_msgs.clear()

app_health_calls: list[str] = []


def fake_app_health(port: str):
    app_health_calls.append(port)
    return {"port": port, "ok": True, "status_code": 200, "latency_ms": 10,
            "healthy": True, "adb": True, "screenshot": True, "ocr": True}


# reset 后真实场景是 adb 设备表重建 → re-probe 看到恢复。
# 这里让 fake_reset 副作用地把 DEVICES_COUNT 切到 2，模拟"reset 真的修好了"。
def fake_reset_with_recovery(reason: str):
    reset_calls.append(reason)
    DEVICES_COUNT["v"] = 2
    return {"ok": True, "reason": reason, "at": wd.utc_now(), "latency_ms": 10,
            "returncode": 0, "stdout": "", "stderr": ""}


wd.http_app_health = fake_app_health
wd.reset_adb_server = fake_reset_with_recovery

DEVICES_COUNT["v"] = 0  # 持续 stale 直到 reset 触发
_ = run(8)  # streak=1
s9 = run(9)  # streak=2 → 触发 reset → fake_reset 把 count 切到 2 → recovered
assert reset_calls and len(reset_calls) == 1, reset_calls
# 强制刷新应已对每个 port 调一次 app_health（迭代号是 9，不在 every=5 周期，证明是 reset 触发的）
assert app_health_calls == PORTS, f"app_health 应在 reset 后被强制调用一次, got {app_health_calls}"
# status 里的 app_health 数组应已更新为新结果（healthy=True×2）
assert len(s9["app_health"]) == 2 and all(r["healthy"] is True for r in s9["app_health"]), s9["app_health"]
assert s9["app_health_checked_at"] is not None
assert any("实例健康：2/2" in m for m in notify_msgs), notify_msgs
print("[6] 自愈成功后立即跑 app_health，obs 立即可见新状态  ✓")

print("\n全部断言通过 ✅  自愈决策路径（全空/部分丢失/阈值/冷却/恢复/强制刷新）行为正确")

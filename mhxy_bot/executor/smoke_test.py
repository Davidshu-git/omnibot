"""
Executor smoke test — 在 NAS 侧运行，全面测试 Windows 执行器所有 API。

用法：
    python mhxy_bot/executor/smoke_test.py [executor_url] [port]

默认：
    executor_url = http://192.168.100.149:8765
    port         = 5557
"""
from __future__ import annotations

import sys
import time

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.100.149:8765"
PORT     = sys.argv[2] if len(sys.argv) > 2 else "5557"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"

_passed = _failed = 0


def post(path: str, timeout: int = 30, **body) -> dict:
    try:
        r = httpx.post(f"{BASE_URL}{path}", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


def check(label: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    sym = PASS if ok else FAIL
    if ok:
        _passed += 1
    else:
        _failed += 1
    print(f"  {sym} {label}" + (f"  [{detail}]" if detail else ""))


def section(title: str) -> None:
    print(f"\n── {title} ──")


# ── 基础连通性 ────────────────────────────────────────────────────────────────
section("基础连通性")
r = httpx.get(f"{BASE_URL}/health", timeout=5)
check("GET /health", r.status_code == 200, r.text[:60])

# ── 现有 API 回归 ─────────────────────────────────────────────────────────────
section("现有 API 回归 — screenshot / sense / tap / back")

d = post("/screenshot", port=PORT)
check("/screenshot 返回 image_b64", "image_b64" in d and len(d.get("image_b64", "")) > 1000,
      f"b64 长度={len(d.get('image_b64', ''))}")

d = post("/sense", port=PORT)
items = d.get("results", [])
check("/sense 返回 results 列表", "results" in d, f"{d.get('count', 0)} 条")
check("/sense 每项含必要字段", all("text" in it and "center_x" in it for it in items) if items else True)

# 取屏幕真实文字，用于后续命中测试
real_texts = [it["text"] for it in items if len(it["text"]) >= 2]
hit_candidate = real_texts[0] if real_texts else None

d = post("/tap", port=PORT, px=800, py=450)
check("/tap 返回 success=True", d.get("success") is True)

d = post("/back", port=PORT)
check("/back 返回 success=True", d.get("success") is True)

# ── batch_tap / batch_back 回归 ───────────────────────────────────────────────
section("现有 API 回归 — batch_tap / batch_back")

d = post("/batch_tap", ports=[PORT], px=800, py=450)
results = d.get("results", {})
check("/batch_tap 返回 results dict", isinstance(results, dict))
check(f"/batch_tap port={PORT} 成功", results.get(PORT) is True)

d = post("/batch_back", ports=[PORT])
results = d.get("results", {})
check("/batch_back 返回 results dict", isinstance(results, dict))
check(f"/batch_back port={PORT} 成功", results.get(PORT) is True)

# ── /swipe ────────────────────────────────────────────────────────────────────
section("新 API — /swipe")

d = post("/swipe", port=PORT, x1=800, y1=700, x2=800, y2=300, duration_ms=300)
check("/swipe 返回 success=True", d.get("success") is True)

# ── /tap_text ─────────────────────────────────────────────────────────────────
section("新 API — /tap_text")

# 未命中路径
d = post("/tap_text", port=PORT, text_candidates=["__不存在__"])
check("未命中：found=False，text/px/py 为 None",
      d.get("found") is False and d.get("text") is None and d.get("px") is None)

# 重新 sense，避免前面 tap/back 改变屏幕后 hit_candidate 失效
# 使用全部当前文字作为候选集，规避 OCR 跨调用的细微差异
d_fresh = post("/sense", port=PORT)
fresh_texts = [it["text"] for it in d_fresh.get("results", []) if len(it["text"]) >= 2]
hit_candidate = fresh_texts[0] if fresh_texts else None

# 命中路径：candidates 覆盖屏幕全部文字，必然有一个被下一次 OCR 命中
if fresh_texts:
    d = post("/tap_text", port=PORT, text_candidates=fresh_texts)
    check(f"命中（{len(fresh_texts)} 个候选）：found=True",
          d.get("found") is True,
          f"text={d.get('text')!r} px={d.get('px')} py={d.get('py')}")
    check("命中时返回整数坐标",
          isinstance(d.get("px"), int) and isinstance(d.get("py"), int))
    check("命中坐标在屏幕范围内",
          0 <= (d.get("px") or -1) <= 1600 and 0 <= (d.get("py") or -1) <= 900)
else:
    print("  ~ 屏幕无可用文字，跳过命中路径")

# ── /wait_text ────────────────────────────────────────────────────────────────
section("新 API — /wait_text")

# tap_text 可能改变了屏幕，重新 sense 取全部当前文字
d_fresh2 = post("/sense", port=PORT)
fresh_texts2 = [it["text"] for it in d_fresh2.get("results", []) if len(it["text"]) >= 2]

# 命中路径：candidates 覆盖屏幕全部文字，必然命中；不测耗时（OCR 每次 ~3s 是硬件限制）
if fresh_texts2:
    d = post("/wait_text", timeout=30, port=PORT,
             text_candidates=fresh_texts2, timeout_sec=10, interval_sec=1.0)
    check(f"命中（{len(fresh_texts2)} 个候选）：found=True",
          d.get("found") is True, f"text={d.get('text')!r}")
    check("命中时返回整数坐标",
          isinstance(d.get("px"), int) and isinstance(d.get("py"), int))
else:
    print("  ~ 屏幕无可用文字，跳过命中路径")

# 超时路径
t0 = time.monotonic()
d = post("/wait_text", timeout=20, port=PORT,
         text_candidates=["__不存在__"], timeout_sec=3, interval_sec=1.0)
elapsed = time.monotonic() - t0
check("超时：found=False", d.get("found") is False)
check(f"超时精度合理 ({elapsed:.1f}s，上限 timeout+8s)", elapsed <= 3 + 8)

# ── /close_common_popups ──────────────────────────────────────────────────────
section("新 API — /close_common_popups")

d = post("/close_common_popups", port=PORT)
check("返回 closed 列表和 count", "closed" in d and "count" in d)
check("count 与 closed 长度一致", d.get("count") == len(d.get("closed", [])))
if d.get("count", 0) > 0:
    first = d["closed"][0]
    check("closed 项含 text/px/py", "text" in first and "px" in first and "py" in first,
          f"关闭了 {d['count']} 个: {[c['text'] for c in d['closed']]}")
else:
    print(f"  ~ 当前无弹窗（count=0，正常）")

# ── /app_health ───────────────────────────────────────────────────────────────
section("新 API — /app_health")

d = post("/app_health", timeout=15, port=PORT)
check("返回 healthy 字段", "healthy" in d)
check("adb=True", d.get("adb") is True)
check("screenshot=True", d.get("screenshot") is True)
check("ocr=True", d.get("ocr") is True)
check("healthy=True（三项全通）", d.get("healthy") is True)

# ── ExecutorClient 封装层 ─────────────────────────────────────────────────────
section("ExecutorClient 封装层")

import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from mhxy_bot.tools.executor_client import ExecutorClient

cli = ExecutorClient(BASE_URL, timeout=30)

check("health()", cli.health() is True)

b64 = cli.screenshot(PORT)
check("screenshot() 返回非空字符串", isinstance(b64, str) and len(b64) > 1000)

ocr = cli.sense(PORT)
check("sense() 返回列表", isinstance(ocr, list))

ok = cli.tap(PORT, 800, 450)
check("tap() 返回 True", ok is True)

ok = cli.back(PORT)
check("back() 返回 True", ok is True)

res = cli.batch_tap([PORT], 800, 450)
check("batch_tap() 返回 dict", isinstance(res, dict) and res.get(PORT) is True)

res = cli.batch_back([PORT])
check("batch_back() 返回 dict", isinstance(res, dict) and res.get(PORT) is True)

ok = cli.swipe(PORT, 800, 700, 800, 300, duration_ms=300)
check("swipe() 返回 True", ok is True)

r = cli.tap_text(PORT, ["__不存在__"])
check("tap_text() 未命中返回 found=False", r.get("found") is False)

if fresh_texts2:
    r = cli.tap_text(PORT, fresh_texts2)
    check(f"tap_text() 命中（{len(fresh_texts2)} 个候选）", r.get("found") is True)

r = cli.wait_text(PORT, ["__不存在__"], timeout_sec=3, interval_sec=1.0)
check("wait_text() 超时返回 found=False", r.get("found") is False)

r = cli.close_common_popups(PORT)
check("close_common_popups() 返回 dict", "closed" in r and "count" in r)

r = cli.app_health(PORT)
check("app_health() healthy=True", r.get("healthy") is True)

# ── 汇总 ──────────────────────────────────────────────────────────────────────
total = _passed + _failed
print(f"\n{'='*40}")
print(f"结果：{_passed}/{total} 通过" + (f"，{_failed} 失败" if _failed else " ✅ 全部通过"))
print(f"{'='*40}\n")
sys.exit(0 if _failed == 0 else 1)

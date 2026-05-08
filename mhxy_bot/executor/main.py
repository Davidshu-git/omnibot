"""
MuMu 执行器服务 — 运行在 Windows 侧，暴露 ADB 操作和 OCR 能力为 HTTP API。
NAS 侧 agent 通过 HTTP 调用，自身不再直接执行 ADB 或本地推理。

启动：
    pip install fastapi uvicorn rapidocr-onnxruntime opencv-python numpy
    python -m uvicorn mhxy_bot.executor.main:app --host 0.0.0.0 --port 8765

或直接运行：
    python mhxy_bot/executor/main.py
"""
from __future__ import annotations

import base64
import faulthandler
import logging
import os
import random
from pathlib import Path
import subprocess
import threading
import time
from typing import Optional
import uuid

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

LOG_DIR = Path(__file__).parent
LOG_FILE = str(LOG_DIR / "executor.log")

_log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

_handler_stderr = logging.StreamHandler()
_handler_stderr.setFormatter(_log_fmt)

_handler_file = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_handler_file.setFormatter(_log_fmt)

log = logging.getLogger("executor")
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(_handler_stderr)
log.addHandler(_handler_file)

app = FastAPI(title="MuMu Executor", version="1.0")

faulthandler.enable()

# 请求统计：每 5 分钟写入日志（用 dict 避免闭包赋值问题）
_req_lock = threading.Lock()
_req_state = {"total": 0, "error": 0, "last_log": time.monotonic()}
_REQ_LOG_INTERVAL = 300
_SLOW_REQ_THRESHOLD = float(os.getenv("EXECUTOR_SLOW_REQ_THRESHOLD_SEC", "8"))


class _StatsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        started = time.monotonic()
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        status_code = 500
        response: Response | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            elapsed = time.monotonic() - started
            log.exception(
                "request error id=%s method=%s path=%s status=%d duration=%.3fs",
                request_id, request.method, request.url.path, status_code, elapsed,
            )
            raise
        finally:
            elapsed = time.monotonic() - started
            if response is not None:
                response.headers["X-Request-ID"] = request_id
            if elapsed >= _SLOW_REQ_THRESHOLD or status_code >= 400:
                log.warning(
                    "request slow_or_error id=%s method=%s path=%s status=%d duration=%.3fs",
                    request_id, request.method, request.url.path, status_code, elapsed,
                )
            elif request.url.path != "/health":
                log.info(
                    "request id=%s method=%s path=%s status=%d duration=%.3fs",
                    request_id, request.method, request.url.path, status_code, elapsed,
                )

            now = time.monotonic()
            with _req_lock:
                _req_state["total"] += 1
                if status_code >= 400:
                    _req_state["error"] += 1
                if now - _req_state["last_log"] >= _REQ_LOG_INTERVAL:
                    log.info("req_stats total=%d errors=%d interval=%.0fs",
                             _req_state["total"], _req_state["error"],
                             now - _req_state["last_log"])
                    _req_state["total"] = 0
                    _req_state["error"] = 0
                    _req_state["last_log"] = now


app.add_middleware(_StatsMiddleware)


@app.on_event("startup")
async def _startup():
    """确保 logger 在 uvicorn 初始化后仍有效（uvicorn 的 dictConfig 可能覆盖）。"""
    if not log.handlers:
        log.addHandler(_handler_stderr)
        log.addHandler(_handler_file)
    log.info("executor started, log_file=%s", LOG_FILE)

ADB_PATH = os.getenv("ADB_PATH", "adb")
W, H = 1600, 900

COMMON_POPUP_TEXTS = ["确定", "关闭", "取消", "稍后", "跳过", "我知道了", "继续"]
DISCONNECTED_STATUS_TEXTS = ["服务器已经关闭", "连接已断开", "网络连接失败", "重新登录"]

# RapidOCR 单例，首次调用时初始化
_ocr = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
        log.info("RapidOCR 已初始化")
    return _ocr


def _port_to_addr(port: str) -> str:
    """将用户端口（如 5557）转换为 MuMu ADB serial（emulator-5556）。
    标准约定：奇数端口为 ADB 传输端口，对应 emulator-(port-1)。
    若已包含 ':' 则直接使用 TCP serial。
    """
    p = str(port).split(":")[-1]  # 兼容 127.0.0.1:5557 格式
    try:
        n = int(p)
        if n % 2 == 1:
            return f"emulator-{n - 1}"
        return f"emulator-{n}"
    except ValueError:
        return port


def _adb(port: str, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    cmd = [ADB_PATH, "-s", _port_to_addr(port)] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("adb timeout port=%s cmd=%s timeout=%ds", port, " ".join(args), timeout)
        raise
    if r.returncode != 0:
        log.warning("adb fail port=%s cmd=%s rc=%d stderr=%s",
                     port, " ".join(args), r.returncode,
                     r.stderr.decode(errors="replace").strip()[:200])
    return r


def _screenshot_png(port: str) -> bytes:
    r = _adb(port, "exec-out", "screencap", "-p")
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"ADB 截图失败：{r.stderr.decode(errors='replace')}")
    return r.stdout


# OCR 结果缓存：{(port,): (timestamp, items)}，TTL 500ms
_ocr_cache: dict[str, tuple[float, list[dict]]] = {}


def _ocr_items(port: str) -> tuple[list[dict], dict]:
    """截图 + OCR，返回 (文字结果列表, timing dict)。"""
    t0 = time.monotonic()
    cached = _ocr_cache.get(port)
    if cached and t0 - cached[0] < 0.5:
        log.debug("ocr cache hit port=%s", port)
        return cached[1], {}

    t1 = time.monotonic()
    png = _screenshot_png(port)
    t2 = time.monotonic()
    arr = np.frombuffer(png, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("图像解码失败")
    img = cv2.resize(img, (1200, 675))
    ocr = _get_ocr()
    result, _ = ocr(img)
    t3 = time.monotonic()
    items = []
    if result:
        for box, text, conf in result:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = float(sum(xs) / 4) * 4 / 3
            cy = float(sum(ys) / 4) * 4 / 3
            items.append({
                "text": text,
                "center_x": cx,
                "center_y": cy,
                "confidence": float(conf),
            })
    timing = {"screenshot_s": round(t2 - t1, 3), "ocr_s": round(t3 - t2, 3), "total_s": round(t3 - t0, 3)}
    _ocr_cache[port] = (t0, items)
    log.info("ocr port=%s text_count=%d screenshot=%.2fs ocr=%.2fs total=%.2fs",
             port, len(items), t2 - t1, t3 - t2, t3 - t0)
    return items, timing


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------

class PortReq(BaseModel):
    port: str

class TapReq(BaseModel):
    port: str
    px: int
    py: int

class BatchTapReq(BaseModel):
    ports: list[str]
    px: int
    py: int

class BatchBackReq(BaseModel):
    ports: list[str]

class SwipeReq(BaseModel):
    port: str
    x1: int
    y1: int
    x2: int
    y2: int
    duration_ms: int = 300

class TapTextReq(BaseModel):
    port: str
    text_candidates: list[str]

class TapTextNearReq(BaseModel):
    port: str
    anchor_candidates: list[str]   # 定位行的锚点文字（如"秘境降妖"）
    text_candidates: list[str]     # 要点击的目标文字（如"参加"）
    prefer_right: bool = True      # 优先选 x > 锚点的项

class WaitTextReq(BaseModel):
    port: str
    text_candidates: list[str]
    timeout_sec: int = 30
    interval_sec: float = 1.5


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    log.info("health check")
    return {"status": "ok", "adb": ADB_PATH}


@app.post("/screenshot")
def screenshot(req: PortReq):
    """截图，返回 base64 编码的 PNG 字节。"""
    try:
        png = _screenshot_png(req.port)
        return {
            "image_b64": base64.b64encode(png).decode(),
            "width": W,
            "height": H,
        }
    except Exception as e:
        log.exception("screenshot error port=%s", req.port)
        raise HTTPException(500, str(e))


@app.post("/sense")
def sense(req: PortReq):
    """截图 + OCR，返回文字和归一化坐标列表。"""
    try:
        items, timing = _ocr_items(req.port)
        return {"results": items, "count": len(items), "timing": timing}
    except Exception as e:
        log.exception("sense error port=%s", req.port)
        raise HTTPException(500, str(e))


@app.post("/tap")
def tap(req: TapReq):
    """ADB 点击像素坐标。"""
    try:
        r = _adb(req.port, "shell", "input", "tap", str(req.px), str(req.py))
        ok = r.returncode == 0
        time.sleep(random.uniform(0.2, 0.4))
        return {"success": ok, "port": req.port, "px": req.px, "py": req.py}
    except Exception as e:
        log.exception("tap error port=%s", req.port)
        raise HTTPException(500, str(e))


@app.post("/back")
def back(req: PortReq):
    """ADB 返回键。"""
    try:
        r = _adb(req.port, "shell", "input", "keyevent", "4")
        ok = r.returncode == 0
        time.sleep(0.3)
        return {"success": ok, "port": req.port}
    except Exception as e:
        log.exception("back error port=%s", req.port)
        raise HTTPException(500, str(e))


@app.post("/batch_tap")
def batch_tap(req: BatchTapReq):
    """批量 ADB 点击，顺序执行。"""
    results: dict[str, bool] = {}
    for port in req.ports:
        try:
            r = _adb(port, "shell", "input", "tap", str(req.px), str(req.py))
            results[port] = r.returncode == 0
        except Exception as e:
            log.warning("batch_tap port=%s error: %s", port, e)
            results[port] = False
        time.sleep(random.uniform(0.3, 0.6))
    return {"results": results}


@app.post("/batch_back")
def batch_back(req: BatchBackReq):
    """批量 ADB 返回键，顺序执行。"""
    results: dict[str, bool] = {}
    for port in req.ports:
        try:
            r = _adb(port, "shell", "input", "keyevent", "4")
            results[port] = r.returncode == 0
        except Exception as e:
            log.warning("batch_back port=%s error: %s", port, e)
            results[port] = False
        time.sleep(0.3)
    return {"results": results}


@app.post("/swipe")
def swipe(req: SwipeReq):
    """ADB 滑动手势。"""
    try:
        r = _adb(
            req.port, "shell", "input", "swipe",
            str(req.x1), str(req.y1), str(req.x2), str(req.y2), str(req.duration_ms),
        )
        ok = r.returncode == 0
        time.sleep(req.duration_ms / 1000 + 0.2)
        return {"success": ok, "port": req.port}
    except Exception as e:
        log.exception("swipe error port=%s", req.port)
        raise HTTPException(500, str(e))


@app.post("/tap_text")
def tap_text(req: TapTextReq):
    """截图 + OCR，找到第一个匹配文本后点击，返回点击坐标和匹配文本。"""
    try:
        items, _ = _ocr_items(req.port)
        for item in items:
            if any(cand in item["text"] for cand in req.text_candidates):
                px = int(item["center_x"])
                py = int(item["center_y"])
                r = _adb(req.port, "shell", "input", "tap", str(px), str(py))
                if r.returncode != 0:
                    raise RuntimeError(f"ADB tap 失败：{r.stderr.decode(errors='replace')}")
                time.sleep(random.uniform(0.2, 0.4))
                log.info("tap_text port=%s matched=%r px=%d py=%d", req.port, item["text"], px, py)
                return {"found": True, "text": item["text"], "px": px, "py": py}
        return {"found": False, "text": None, "px": None, "py": None}
    except Exception as e:
        log.exception("tap_text error port=%s", req.port)
        raise HTTPException(500, str(e))


@app.post("/tap_text_near")
def tap_text_near(req: TapTextNearReq):
    """截图 + OCR，找到与锚点同行的目标文字并点击。

    先找锚点文字确定行的 center_y，再找所有匹配目标文字中
    y 距离最近（且 prefer_right 时 x > 锚点）的那个点击。
    """
    try:
        items, _ = _ocr_items(req.port)

        anchor = next(
            (it for it in items if any(c in it["text"] for c in req.anchor_candidates)),
            None,
        )
        if anchor is None:
            return {"found": False, "reason": "anchor_not_found", "text": None, "px": None, "py": None}

        anchor_x, anchor_y = anchor["center_x"], anchor["center_y"]

        candidates = [
            it for it in items
            if any(c in it["text"] for c in req.text_candidates)
        ]
        if req.prefer_right:
            right = [it for it in candidates if it["center_x"] > anchor_x]
            if right:
                candidates = right

        if not candidates:
            return {"found": False, "reason": "target_not_found", "text": None, "px": None, "py": None}

        target = min(candidates, key=lambda it: abs(it["center_y"] - anchor_y))
        px = int(target["center_x"])
        py = int(target["center_y"])
        r = _adb(req.port, "shell", "input", "tap", str(px), str(py))
        if r.returncode != 0:
            raise RuntimeError(f"ADB tap 失败：{r.stderr.decode(errors='replace')}")
        time.sleep(random.uniform(0.2, 0.4))
        log.info("tap_text_near port=%s anchor=%r matched=%r px=%d py=%d",
                 req.port, anchor["text"], target["text"], px, py)
        return {"found": True, "text": target["text"], "px": px, "py": py}
    except Exception as e:
        log.exception("tap_text_near error port=%s", req.port)
        raise HTTPException(500, str(e))


@app.post("/wait_text")
def wait_text(req: WaitTextReq):
    """循环 OCR 直到任一候选文本出现或超时，返回是否命中、命中文本和坐标。"""
    deadline = time.monotonic() + req.timeout_sec
    try:
        while time.monotonic() < deadline:
            items, _ = _ocr_items(req.port)
            for item in items:
                if any(cand in item["text"] for cand in req.text_candidates):
                    log.info("wait_text port=%s matched=%r", req.port, item["text"])
                    return {
                        "found": True,
                        "text": item["text"],
                        "px": int(item["center_x"]),
                        "py": int(item["center_y"]),
                    }
            time.sleep(req.interval_sec)
        log.info("wait_text port=%s timeout after %ds", req.port, req.timeout_sec)
        return {"found": False, "text": None, "px": None, "py": None}
    except Exception as e:
        log.exception("wait_text error port=%s", req.port)
        raise HTTPException(500, str(e))


@app.post("/close_common_popups")
def close_common_popups(req: PortReq):
    """识别并点击常见弹窗按钮，返回关闭了哪些弹窗。

    只点击明确按钮型文本；断线/服务器关闭类文本作为状态信号处理，
    不在这里点击，避免误触。
    """
    try:
        items, _ = _ocr_items(req.port)
        closed: list[dict] = []
        for item in items:
            if any(popup in item["text"] for popup in COMMON_POPUP_TEXTS):
                px = int(item["center_x"])
                py = int(item["center_y"])
                r = _adb(req.port, "shell", "input", "tap", str(px), str(py))
                if r.returncode == 0:
                    closed.append({"text": item["text"], "px": px, "py": py})
                    log.info("close_common_popups port=%s closed=%r", req.port, item["text"])
                    time.sleep(random.uniform(0.3, 0.5))
        return {"closed": closed, "count": len(closed)}
    except Exception as e:
        log.exception("close_common_popups error port=%s", req.port)
        raise HTTPException(500, str(e))


@app.post("/app_health")
def app_health(req: PortReq):
    """检查 ADB 连通性、截图和 OCR 是否可用，返回实例健康状态。"""
    adb_ok = False
    screenshot_ok = False
    ocr_ok = False
    details: dict[str, str] = {}

    try:
        r = _adb(req.port, "get-state", timeout=5)
        adb_ok = r.returncode == 0 and b"device" in r.stdout
        if not adb_ok:
            details["adb"] = (r.stdout.decode(errors="replace").strip()
                              or r.stderr.decode(errors="replace").strip())
    except Exception as e:
        details["adb"] = str(e)

    if adb_ok:
        try:
            _screenshot_png(req.port)
            screenshot_ok = True
        except Exception as e:
            details["screenshot"] = str(e)

    if screenshot_ok:
        try:
            _get_ocr()
            ocr_ok = True
        except Exception as e:
            details["ocr"] = str(e)

    healthy = adb_ok and screenshot_ok and ocr_ok
    log.info("app_health port=%s healthy=%s adb=%s screenshot=%s ocr=%s",
             req.port, healthy, adb_ok, screenshot_ok, ocr_ok)
    return {
        "healthy": healthy,
        "port": req.port,
        "adb": adb_ok,
        "screenshot": screenshot_ok,
        "ocr": ocr_ok,
        "details": details,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("EXECUTOR_PORT", "8765")))

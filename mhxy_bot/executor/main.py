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
import logging
import os
import random
import subprocess
import time
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="MuMu Executor", version="1.0")

ADB_PATH = os.getenv("ADB_PATH", "adb")
W, H = 1600, 900

COMMON_POPUP_TEXTS = ["确定", "关闭", "取消", "稍后", "跳过", "我知道了", "继续", "服务器已经关闭"]

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
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _screenshot_png(port: str) -> bytes:
    r = _adb(port, "exec-out", "screencap", "-p")
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"ADB 截图失败：{r.stderr.decode(errors='replace')}")
    return r.stdout


def _ocr_items(port: str) -> list[dict]:
    """截图 + OCR，返回文字结果列表，每项含 text / center_x / center_y / confidence。"""
    png = _screenshot_png(port)
    arr = np.frombuffer(png, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("图像解码失败")
    ocr = _get_ocr()
    result, _ = ocr(img)
    items = []
    if result:
        for box, text, conf in result:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = float(sum(xs) / 4)
            cy = float(sum(ys) / 4)
            items.append({
                "text": text,
                "center_x": cx,
                "center_y": cy,
                "confidence": float(conf),
            })
    return items


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
        log.error("screenshot error port=%s: %s", req.port, e)
        raise HTTPException(500, str(e))


@app.post("/sense")
def sense(req: PortReq):
    """截图 + OCR，返回文字和归一化坐标列表。"""
    try:
        items = _ocr_items(req.port)
        return {"results": items, "count": len(items)}
    except Exception as e:
        log.error("sense error port=%s: %s", req.port, e)
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
        log.error("tap error port=%s: %s", req.port, e)
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
        log.error("back error port=%s: %s", req.port, e)
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
        log.error("swipe error port=%s: %s", req.port, e)
        raise HTTPException(500, str(e))


@app.post("/tap_text")
def tap_text(req: TapTextReq):
    """截图 + OCR，找到第一个匹配文本后点击，返回点击坐标和匹配文本。"""
    try:
        items = _ocr_items(req.port)
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
        log.error("tap_text error port=%s: %s", req.port, e)
        raise HTTPException(500, str(e))


@app.post("/wait_text")
def wait_text(req: WaitTextReq):
    """循环 OCR 直到任一候选文本出现或超时，返回是否命中、命中文本和坐标。"""
    deadline = time.monotonic() + req.timeout_sec
    try:
        while time.monotonic() < deadline:
            items = _ocr_items(req.port)
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
        log.error("wait_text error port=%s: %s", req.port, e)
        raise HTTPException(500, str(e))


@app.post("/close_common_popups")
def close_common_popups(req: PortReq):
    """识别并点击常见弹窗按钮，返回关闭了哪些弹窗。"""
    try:
        items = _ocr_items(req.port)
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
        log.error("close_common_popups error port=%s: %s", req.port, e)
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

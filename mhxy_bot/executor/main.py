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
        png = _screenshot_png(req.port)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("EXECUTOR_PORT", "8765")))

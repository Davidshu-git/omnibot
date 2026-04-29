"""NAS 侧执行器 HTTP 客户端，封装对 Windows 执行器服务的调用。"""
from __future__ import annotations

import httpx


class ExecutorClient:
    def __init__(self, base_url: str, timeout: int = 30) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _post(self, path: str, *, _timeout: int | None = None, **body) -> dict:
        r = httpx.post(
            f"{self._base}{path}",
            json=body,
            timeout=_timeout if _timeout is not None else self._timeout,
        )
        r.raise_for_status()
        return r.json()

    def health(self) -> bool:
        try:
            r = httpx.get(f"{self._base}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 基础操作
    # ------------------------------------------------------------------

    def screenshot(self, port: str) -> str:
        """返回 base64 编码的 PNG 字节串。"""
        return self._post("/screenshot", port=port)["image_b64"]

    def sense(self, port: str) -> list[dict]:
        """返回 OCR 结果列表，每项含 text / center_x / center_y / confidence。"""
        return self._post("/sense", port=port)["results"]

    def tap(self, port: str, px: int, py: int) -> bool:
        return self._post("/tap", port=port, px=px, py=py)["success"]

    def back(self, port: str) -> bool:
        return self._post("/back", port=port)["success"]

    def batch_tap(self, ports: list[str], px: int, py: int) -> dict[str, bool]:
        return self._post("/batch_tap", ports=ports, px=px, py=py)["results"]

    def batch_back(self, ports: list[str]) -> dict[str, bool]:
        return self._post("/batch_back", ports=ports)["results"]

    # ------------------------------------------------------------------
    # 高层动作
    # ------------------------------------------------------------------

    def swipe(self, port: str, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 300) -> bool:
        """ADB 滑动手势，返回是否成功。"""
        return self._post(
            "/swipe",
            port=port, x1=x1, y1=y1, x2=x2, y2=y2, duration_ms=duration_ms,
        )["success"]

    def tap_text(self, port: str, text_candidates: list[str]) -> dict:
        """截图 + OCR，点击第一个匹配的文本。

        返回：
            {"found": bool, "text": str | None, "px": int | None, "py": int | None}
        """
        return self._post("/tap_text", port=port, text_candidates=text_candidates)

    def wait_text(self, port: str, text_candidates: list[str],
                  timeout_sec: int = 30, interval_sec: float = 1.5) -> dict:
        """循环 OCR 等待任一候选文本出现。

        返回：
            {"found": bool, "text": str | None, "px": int | None, "py": int | None}
        """
        return self._post(
            "/wait_text",
            _timeout=timeout_sec + 15,
            port=port,
            text_candidates=text_candidates,
            timeout_sec=timeout_sec,
            interval_sec=interval_sec,
        )

    def close_common_popups(self, port: str) -> dict:
        """识别并关闭常见弹窗（确定/关闭/取消/跳过等）。

        返回：
            {"closed": [{"text": str, "px": int, "py": int}, ...], "count": int}
        """
        return self._post("/close_common_popups", port=port)

    def app_health(self, port: str) -> dict:
        """检查指定实例的 ADB / 截图 / OCR 健康状态。

        返回：
            {"healthy": bool, "port": str, "adb": bool,
             "screenshot": bool, "ocr": bool, "details": dict}
        """
        return self._post("/app_health", _timeout=15, port=port)

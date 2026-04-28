"""NAS 侧执行器 HTTP 客户端，封装对 Windows 执行器服务的调用。"""
from __future__ import annotations

import httpx


class ExecutorClient:
    def __init__(self, base_url: str, timeout: int = 30) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _post(self, path: str, **body) -> dict:
        r = httpx.post(f"{self._base}{path}", json=body, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def health(self) -> bool:
        try:
            r = httpx.get(f"{self._base}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

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

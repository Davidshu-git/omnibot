"""NAS 侧执行器 HTTP 客户端，封装对 Windows 执行器服务的调用。"""
from __future__ import annotations

import contextvars
import uuid
from typing import Callable

import httpx


_session_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mhxy_executor_session_id", default=None
)
_trace_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mhxy_executor_trace_id", default=None
)


def set_executor_context(*, session_id: str | None = None, trace_id: str | None = None) -> None:
    """设置当前上下文的 executor trace/session header。"""
    _session_id_ctx.set(session_id)
    _trace_id_ctx.set(trace_id)


class ExecutorClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = 30,
        *,
        session_id: str | None = None,
        trace_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session_id = session_id
        self._trace_id_provider = trace_id_provider

    def set_context(self, *, session_id: str | None = None, trace_id: str | None = None) -> None:
        """设置当前执行上下文，用于向 Windows executor 透传 obs 关联 header。"""
        set_executor_context(session_id=session_id, trace_id=trace_id)

    def _headers(self) -> dict[str, str]:
        """构造每次请求的 trace/session header。"""
        headers = {"X-Request-ID": uuid.uuid4().hex[:12]}
        session_id = _session_id_ctx.get() or self._session_id
        if session_id:
            headers["X-Session-Id"] = session_id

        trace_id = _trace_id_ctx.get()
        if trace_id is None and self._trace_id_provider is not None:
            trace_id = self._trace_id_provider()
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        return headers

    def _post(self, path: str, *, _timeout: int | None = None, **body) -> dict:
        r = httpx.post(
            f"{self._base}{path}",
            json=body,
            timeout=_timeout if _timeout is not None else self._timeout,
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    def _get(self, path: str, *, _timeout: int | None = None) -> dict:
        r = httpx.get(
            f"{self._base}{path}",
            timeout=_timeout if _timeout is not None else self._timeout,
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    def health(self) -> bool:
        try:
            r = httpx.get(f"{self._base}/health", timeout=5, headers=self._headers())
            return r.status_code == 200
        except Exception:
            return False

    def list_devices(self) -> dict:
        """从 Windows executor 查询当前 ADB 可见的模拟器端口列表。

        返回：
            {"ports": list[int], "count": int}
        """
        return self._get("/list_devices", _timeout=15)

    # ------------------------------------------------------------------
    # 基础操作
    # ------------------------------------------------------------------

    def screenshot(self, port: str) -> str:
        """返回 base64 编码的 PNG 字节串。"""
        return self._post("/screenshot", port=port)["image_b64"]

    def sense(self, port: str) -> list[dict]:
        """返回 OCR 结果列表，每项含 text / center_x / center_y / confidence。"""
        return self._post("/sense", port=port)["results"]

    def sense_with_timing(self, port: str) -> dict:
        """返回完整 /sense 响应（含 results / count / timing）。"""
        return self._post("/sense", port=port)

    def tap(self, port: str, px: int, py: int) -> bool:
        return self._post("/tap", port=port, px=px, py=py)["success"]

    def back(self, port: str) -> bool:
        return self._post("/back", port=port)["success"]

    def batch_tap(self, ports: list[str], px: int, py: int) -> dict[str, bool]:
        return self._post("/batch_tap", ports=ports, px=px, py=py)["results"]

    def batch_swipe(
        self,
        ports: list[str],
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> dict[str, bool]:
        return self._post(
            "/batch_swipe",
            ports=ports,
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            duration_ms=duration_ms,
            _timeout=max(self._timeout, int(len(ports) * (duration_ms / 1000 + 2) + 5)),
        )["results"]

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

    def tap_text_near(self, port: str, anchor_candidates: list[str],
                      text_candidates: list[str], prefer_right: bool = True) -> dict:
        """找到与锚点同行的目标文字并点击。

        返回：
            {"found": bool, "text": str | None, "px": int | None, "py": int | None}
        """
        return self._post(
            "/tap_text_near",
            port=port,
            anchor_candidates=anchor_candidates,
            text_candidates=text_candidates,
            prefer_right=prefer_right,
        )

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

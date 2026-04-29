"""感知层：屏幕状态检测、文本探测、denylist 扫描。"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from mhxy_bot.runner.models import InstanceState

if TYPE_CHECKING:
    from mhxy_bot.runner.context import RunnerContext

# ---------------------------------------------------------------------------
# Denylist — 检测到任一词时任务必须暂停
# ---------------------------------------------------------------------------

DENYLIST: list[str] = [
    "充值", "购买", "摆摊", "交易", "赠送",
    "分解", "删除", "改名", "账号", "安全",
]

# ---------------------------------------------------------------------------
# 主界面特征文字（用于 detect_screen_state）
# ---------------------------------------------------------------------------

_MAIN_UI_MARKERS = ["背包", "地图", "任务", "商城", "设置"]
_LOGIN_MARKERS   = ["登录", "账号登录", "游客登录"]
_DISCONNECTED_MARKERS = ["服务器已经关闭", "连接已断开", "网络连接失败", "重新登录"]
_BATTLE_MARKERS  = ["战斗", "回合", "行动", "攻击", "技能"]
_TEAM_MARKERS    = ["队伍", "队长", "队员", "组队"]
_POPUP_MARKERS   = ["确定", "关闭", "取消", "我知道了"]


def _sense(ctx: "RunnerContext") -> list[dict]:
    """调用执行器 OCR，返回文字列表；失败时返回空列表。"""
    try:
        return ctx.executor.sense(ctx.port)
    except Exception as exc:
        ctx.warning("sense failed: %s", exc)
        return []


def _texts(items: list[dict]) -> list[str]:
    return [it["text"] for it in items]


def detect_screen_state(ctx: "RunnerContext") -> InstanceState:
    """根据 OCR 文字推断当前屏幕状态。

    断线类文本只作为状态信号，不参与可点击弹窗按钮识别。
    """
    items = _sense(ctx)
    if not items:
        return InstanceState.OFFLINE

    texts = _texts(items)
    joined = "".join(texts)

    if any(m in joined for m in _DISCONNECTED_MARKERS):
        return InstanceState.DISCONNECTED
    if any(m in joined for m in _BATTLE_MARKERS):
        return InstanceState.IN_BATTLE
    if any(m in joined for m in _POPUP_MARKERS):
        return InstanceState.POPUP
    if any(m in joined for m in _TEAM_MARKERS):
        return InstanceState.IN_TEAM
    if any(m in joined for m in _MAIN_UI_MARKERS):
        return InstanceState.MAIN_UI
    if any(m in joined for m in _LOGIN_MARKERS):
        return InstanceState.LOGIN_SCREEN
    return InstanceState.UNKNOWN


def has_text(ctx: "RunnerContext", candidates: list[str]) -> bool:
    """屏幕上是否存在任一候选文本。"""
    items = _sense(ctx)
    texts = _texts(items)
    joined = " ".join(texts)
    return any(c in joined for c in candidates)


def wait_until(
    ctx: "RunnerContext",
    condition: Callable[["RunnerContext"], bool],
    timeout_sec: int = 30,
    interval_sec: float = 1.5,
) -> bool:
    """轮询直到 condition 为 True 或超时，返回是否成功。"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if ctx.stop_requested:
            return False
        if condition(ctx):
            return True
        time.sleep(interval_sec)
    return False


def detect_common_popup(ctx: "RunnerContext") -> list[str]:
    """返回当前屏幕上匹配常见弹窗按钮的文本列表（不点击）。"""
    from mhxy_bot.executor.main import COMMON_POPUP_TEXTS
    items = _sense(ctx)
    joined = " ".join(_texts(items))
    return [p for p in COMMON_POPUP_TEXTS if p in joined]


def detect_denylisted_screen(ctx: "RunnerContext") -> list[str]:
    """返回当前屏幕上匹配 denylist 的词列表；非空即需暂停。"""
    items = _sense(ctx)
    joined = " ".join(_texts(items))
    return [w for w in DENYLIST if w in joined]


def classify_by_vl(ctx: "RunnerContext", prompt: str) -> str:
    """用 VL 模型描述当前屏幕（需要 NAS 侧有 OpenAI 客户端配置）。

    此函数为可选能力，调用方应处理 ImportError / RuntimeError。
    """
    import base64
    import os
    from openai import OpenAI

    img_b64 = ctx.executor.screenshot(ctx.port)
    client = OpenAI(
        api_key=os.getenv("VL_DASHSCOPE_API_KEY") or os.getenv("DASHSCOPE_API_KEY", ""),
        base_url=os.getenv("VL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    vl_model = os.getenv("MHXY_VL_MODEL", "qwen3-vl-plus")
    resp = client.chat.completions.create(
        model=vl_model,
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        max_tokens=512,
    )
    return resp.choices[0].message.content or ""

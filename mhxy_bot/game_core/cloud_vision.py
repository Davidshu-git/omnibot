# -*- coding: utf-8 -*-
"""
云端视觉分析 - Qwen3-VL-Plus
使用阿里云通义千问视觉模型分析游戏截图
"""
import os
import json
import base64
import time
from typing import Dict, Any, Optional
import logging
from dotenv import dotenv_values
from openai import OpenAI
from mhxy_bot.config import QWEN_VL_PLUS_MODEL, DEFAULT_RESOLUTION

logger = logging.getLogger(__name__)

# 线程局部变量，用于存储当前的日志回调 handler
# 使用 contextvars 而非 threading.local()，以支持 LangChain 的 run_in_executor 场景
import contextvars
_current_log_callback: contextvars.ContextVar = contextvars.ContextVar("vl_log_callback", default=None)


def set_log_callback(callback) -> None:
    """设置当前上下文的日志回调 handler（由 game_tools.py 在调用工具时设置）。"""
    _current_log_callback.set(callback)


def get_log_callback():
    """获取当前上下文的日志回调 handler。"""
    return _current_log_callback.get()


def _log_vl_call(
    model: str,
    duration_ms: float,
    success: bool,
    error: Optional[str] = None,
    usage: Optional[dict] = None,
    prompt: Optional[str] = None,
    raw_output: Optional[str] = None,
) -> None:
    """
    记录 VL 模型调用日志到当前 session 日志文件（格式与 SessionLogCallbackHandler.on_llm_end 一致）。
    prompt / raw_output 有值时写入，供日志监控面板展示。
    """
    log_callback = get_log_callback()
    if not log_callback or not hasattr(log_callback, '_write_log'):
        return

    try:
        entry = {
            "type": "model_call",
            "timestamp": log_callback._now() if hasattr(log_callback, '_now') else "",
            "model": model,
            "input_tokens": (usage or {}).get("input_tokens", 0),
            "output_tokens": (usage or {}).get("output_tokens", 0),
            "cache_read_tokens": (usage or {}).get("cache_read_tokens"),
            "cache_write_tokens": (usage or {}).get("cache_write_tokens"),
            "total_tokens": (usage or {}).get("total_tokens", 0),
            "duration_ms": round(duration_ms, 2),
            "stop_reason": "error" if error else "",
            "error_message": error,
        }
        if prompt:
            entry["prompt"] = prompt[:5000]
        if raw_output:
            entry["raw_output"] = raw_output[:5000]
        log_callback._write_log(entry)
    except Exception:
        pass


class CloudVisionAnalyzer:
    """
    云端视觉分析器

    功能：
    - 使用 Qwen3-VL-Plus 分析游戏截图
    - 识别 UI 按钮和坐标（支持 function calling 结构化输出）
    - 理解游戏场景
    """

    def __init__(self, api_key: str = None, log_callback=None):
        """
        初始化分析器

        Args:
            api_key: DashScope API 密钥
            log_callback: 日志回调 handler
        """
        self.api_key = api_key or os.getenv("VL_DASHSCOPE_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        self.base_url = os.getenv("VL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

        if not self.api_key:
            env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
            if os.path.exists(env_file):
                env_vals = dotenv_values(env_file)
                self.api_key = env_vals.get("VL_DASHSCOPE_API_KEY") or env_vals.get("DASHSCOPE_API_KEY")

        if not self.api_key:
            raise ValueError("未设置 VL_DASHSCOPE_API_KEY 或 DASHSCOPE_API_KEY")

        self.model = QWEN_VL_PLUS_MODEL
        self.log_callback = log_callback
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _call_vl(self, image_path: str, prompt: str, json_mode: bool = False) -> Dict[str, Any]:
        """调用 VL API，返回 {"success": True, "content": str} 或 {"success": False, "error": str}"""
        start_time = time.perf_counter()
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            ext = os.path.splitext(image_path)[1].lower().lstrip(".")
            mime = "image/png" if ext in ("png", "") else f"image/{ext}"

            kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ]}],
                **kwargs
            )

            duration_ms = (time.perf_counter() - start_time) * 1000
            usage = {}
            if response.usage:
                cache_read = None
                ptd = getattr(response.usage, "prompt_tokens_details", None)
                if ptd is not None:
                    cache_read = getattr(ptd, "cached_tokens", None)
                usage = {
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cache_read_tokens": cache_read,
                }
            content = response.choices[0].message.content or ""
            _log_vl_call(self.model, duration_ms, success=True, usage=usage, prompt=prompt, raw_output=content[:5000])
            return {"success": True, "content": content}

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"[FAIL] VL 调用异常：{e}")
            _log_vl_call(self.model, duration_ms, success=False, error=str(e), prompt=prompt)
            return {"success": False, "error": str(e)}

    def locate_element(self, image_path: str, target: str = None) -> Dict[str, Any]:
        """
        识别截图中指定元素的归一化坐标。

        Returns:
            {"success": True, "elements": [...]} 或 {"success": False, "error": "..."}
        """
        W, H = DEFAULT_RESOLUTION
        if target:
            prompt = (
                f"这是梦幻西游手游的截图（分辨率 {W}x{H}）。"
                f"请找出画面中与「{target}」相关的 UI 元素（按钮、图标、标签等均可）。\n"
                f"用 JSON 返回，格式：{{\"elements\": [{{\"name\": \"元素名\", \"x\": 归一化x(0-1), \"y\": 归一化y(0-1)}}]}}\n"
                f"如果找不到，返回 {{\"elements\": []}}。只返回 JSON，不要其他文字。"
            )
        else:
            prompt = (
                f"这是梦幻西游手游的截图（分辨率 {W}x{H}）。"
                f"请识别画面中所有可点击的 UI 元素。\n"
                f"用 JSON 返回，格式：{{\"elements\": [{{\"name\": \"元素名\", \"x\": 归一化x(0-1), \"y\": 归一化y(0-1)}}]}}\n"
                f"只返回 JSON，不要其他文字。"
            )

        logger.info(f"[INFO] 正在调用 {self.model} 识别元素...")
        resp = self._call_vl(image_path, prompt, json_mode=True)
        if not resp["success"]:
            return {**resp, "model": self.model}

        try:
            data = json.loads(resp["content"])
            elements = data.get("elements", [])
            logger.info(f"[OK] 识别到 {len(elements)} 个元素")
            return {"success": True, "elements": elements, "model": self.model}
        except Exception as e:
            logger.warning(f"[WARN] JSON 解析失败：{e}，原始内容：{resp['content'][:200]}")
            return {"success": False, "error": f"JSON 解析失败：{e}", "model": self.model}

    def analyze_scene(self, image_path: str, prompt: str = None) -> Dict[str, Any]:
        """
        分析游戏场景

        Args:
            image_path: 截图文件路径
            prompt: 自定义提示词（可选）

        Returns:
            分析结果
        """
        if not prompt:
            prompt = """请详细分析这张梦幻西游手游的截图，包括：
1. 游戏场景（在哪里）
2. 角色信息（等级、门派、外观）
3. 界面元素（打开了哪些功能按钮）
4. 任务信息（当前有什么任务）
5. 其他重要信息（活动、聊天等）"""

        logger.info("[INFO] 正在分析场景...")
        resp = self._call_vl(image_path, prompt)
        if not resp["success"]:
            return {**resp, "model": self.model}
        return {"success": True, "result": resp["content"], "model": self.model}

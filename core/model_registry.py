"""
模型注册中心 - 统一管理多 LLM 配置、持久化当前选择、提供 ChatOpenAI 实例构建。
"""
import json
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Type

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    key: str
    display_name: str
    api_key: str        # 已从 env 解析的值
    base_url: str
    model: str
    extra_body: dict | None = None
    timeout: int = 90
    max_tokens: int = 8192
    llm_class: Type[ChatOpenAI] = ChatOpenAI


class ModelRegistry:
    """
    持有多个 ModelConfig，管理当前激活的 key，支持持久化到 JSON 文件。

    Args:
        configs:       ModelConfig 列表
        settings_path: 持久化文件路径（如 data/stock/model_settings.json）
        default_key:   默认选中的 model key
    """

    _SETTINGS_FILE = "model_settings.json"

    def __init__(
        self,
        configs: list[ModelConfig],
        settings_path: Path,
        default_key: str,
    ) -> None:
        self._configs: dict[str, ModelConfig] = {c.key: c for c in configs}
        self._settings_path = settings_path
        self._default_key = default_key
        self._llm_cache: dict[str, ChatOpenAI] = {}

        # 确保目录存在
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)

        # 从文件加载持久化 key，若文件不存在则使用 default
        self._current_key = self._load_key()

    # ------------------------------------------------------------------
    # 内部持久化
    # ------------------------------------------------------------------

    def _load_key(self) -> str:
        try:
            if self._settings_path.exists():
                data = json.loads(self._settings_path.read_text(encoding="utf-8"))
                key = data.get("model_key", self._default_key)
                if key in self._configs:
                    return key
        except Exception as e:
            logger.warning(f"[ModelRegistry] 读取 settings 失败，使用默认 key：{e}")
        return self._default_key

    def _save_key(self, key: str) -> None:
        try:
            self._settings_path.write_text(
                json.dumps({"model_key": key}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[ModelRegistry] 写入 settings 失败：{e}")

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def list_models(self) -> list[ModelConfig]:
        return list(self._configs.values())

    def current_key(self) -> str:
        return self._current_key

    def current(self) -> ModelConfig:
        return self._configs[self._current_key]

    def switch(self, key: str) -> None:
        """切换到指定 model key，并持久化到文件。"""
        if key not in self._configs:
            raise ValueError(f"未知模型 key：{key!r}，可选：{list(self._configs.keys())}")
        self._current_key = key
        self._save_key(key)
        logger.info(f"[ModelRegistry] 已切换到模型：{key}")

    def build_llm(self, key: str | None = None) -> ChatOpenAI:
        """
        构建（或从缓存返回）指定 key 的 ChatOpenAI 实例。
        key 为 None 时使用当前激活的 key。
        """
        resolved_key = key if key is not None else self._current_key
        if resolved_key not in self._configs:
            raise ValueError(f"未知模型 key：{resolved_key!r}")

        if resolved_key not in self._llm_cache:
            cfg = self._configs[resolved_key]
            if not cfg.api_key:
                raise RuntimeError(
                    f"模型 {resolved_key!r} 的 API Key 未配置，无法构建 LLM。"
                )
            llm = cfg.llm_class(
                model=cfg.model,
                api_key=SecretStr(cfg.api_key),
                base_url=cfg.base_url,
                temperature=0.2,
                timeout=cfg.timeout,
                max_retries=3,
                max_tokens=cfg.max_tokens,
                stream_usage=True,
                **({"extra_body": cfg.extra_body} if cfg.extra_body else {}),
            )
            self._llm_cache[resolved_key] = llm
            logger.info(f"[ModelRegistry] 已构建 LLM 实例：{resolved_key}")

        return self._llm_cache[resolved_key]


def make_standard_registry(settings_dir: Path) -> ModelRegistry:
    """
    从环境变量读取 API Key，构造三个标准 ModelConfig，返回 ModelRegistry。
    settings 文件写入 settings_dir/model_settings.json。
    若某个 key 的 api_key 为空，仍注册但 build_llm 时会报错。
    """
    from core.deepseek_llm import DeepSeekChatLLM

    configs = [
        ModelConfig(
            key="minimax",
            display_name="MiniMax M2.7",
            api_key=os.getenv("MINIMAX_API_KEY", ""),
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            timeout=90,
            max_tokens=8192,
        ),
        ModelConfig(
            key="qwen",
            display_name="Qwen 3.5 Plus",
            api_key=os.getenv("ALI_CODING_PLAN_KEY", ""),
            base_url="https://coding.dashscope.aliyuncs.com/v1",
            model="qwen3.5-plus",
            timeout=90,
            max_tokens=8192,
        ),
        ModelConfig(
            key="deepseek",
            display_name="DeepSeek V4 Flash",
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            timeout=90,
            max_tokens=8192,
            llm_class=DeepSeekChatLLM,
        ),
    ]

    settings_path = settings_dir / "model_settings.json"
    return ModelRegistry(configs=configs, settings_path=settings_path, default_key="minimax")

"""VL 视觉模型注册中心 — 管理可切换的视觉模型列表，持久化当前选择。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class VlModelConfig:
    key: str
    display_name: str
    model: str


class VlModelRegistry:
    def __init__(
        self,
        configs: list[VlModelConfig],
        settings_path: Path,
        default_key: str,
    ) -> None:
        self._configs: dict[str, VlModelConfig] = {c.key: c for c in configs}
        self._settings_path = settings_path
        self._default_key = default_key
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._current_key = self._load_key()

    def _load_key(self) -> str:
        try:
            if self._settings_path.exists():
                data = json.loads(self._settings_path.read_text(encoding="utf-8"))
                key = data.get("model_key", self._default_key)
                if key in self._configs:
                    return key
        except Exception as e:
            log.warning("[VlModelRegistry] 读取 settings 失败：%s", e)
        return self._default_key

    def _save_key(self, key: str) -> None:
        try:
            self._settings_path.write_text(
                json.dumps({"model_key": key}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.error("[VlModelRegistry] 写入 settings 失败：%s", e)

    def current_key(self) -> str:
        return self._current_key

    def current(self) -> VlModelConfig:
        return self._configs[self._current_key]

    def current_model(self) -> str:
        return self.current().model

    def list_models(self) -> list[VlModelConfig]:
        return list(self._configs.values())

    def switch(self, key: str) -> None:
        if key not in self._configs:
            raise ValueError(f"未知模型 key：{key!r}，可选：{list(self._configs.keys())}")
        self._current_key = key
        self._save_key(key)
        log.info("[VlModelRegistry] 已切换到：%s", key)

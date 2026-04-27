# -*- coding: utf-8 -*-
"""
UI 元素库

存储和管理游戏界面中的可交互元素位置信息
支持持久化存储到 JSON 文件
"""
import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from mhxy_bot.config import DEFAULT_RESOLUTION, ELEMENT_LIBRARY_JSON


class ElementLibrary:
    """
    UI 元素库

    功能：
    - 存储元素信息（名称、归一化坐标、说明）
    - 按名称查找元素
    - 按类型查找元素
    - 支持多分辨率适配
    """

    def __init__(self, config_file: str = None):
        """
        初始化元素库

        Args:
            config_file: 配置文件路径（可选，默认从 config/element_library.json 读取）
        """
        self.elements: Dict[str, Dict[str, Any]] = {}

        self.config_file = str(config_file or ELEMENT_LIBRARY_JSON)

        if ELEMENT_LIBRARY_JSON.exists() if config_file is None else Path(self.config_file).exists():
            self.load_from_file(self.config_file)
        else:
            self.save_to_file(self.config_file)

    def get_element(self, name: str) -> Optional[Dict[str, Any]]:
        """按名称获取元素信息，支持模糊匹配"""
        if name in self.elements:
            return self.elements[name]
        for elem_name, info in self.elements.items():
            if name in elem_name or elem_name in name:
                return info
        return None

    def get_elements_by_type(self, elem_type: str) -> List[Dict[str, Any]]:
        """按类型获取元素列表"""
        return [
            {"name": name, **info}
            for name, info in self.elements.items()
            if info.get("type") == elem_type
        ]

    def get_all_elements(self) -> Dict[str, Dict[str, Any]]:
        """获取所有元素"""
        return self.elements

    def add_element(self, name: str, x: float, y: float,
                   description: str = "", elem_type: str = "unknown",
                   resolution: str = f"{DEFAULT_RESOLUTION[1]}x{DEFAULT_RESOLUTION[0]}",
                   center: Dict = None, bbox: Dict = None, template: str = None):
        """添加新元素"""
        self.elements[name] = {
            "x": x,
            "y": y,
            "description": description,
            "type": elem_type,
            "resolution": resolution
        }
        if center:
            self.elements[name]["center"] = center
        if bbox:
            self.elements[name]["bbox"] = bbox
        if template:
            self.elements[name]["template"] = template

    def update_element(self, name: str, **kwargs):
        """更新元素信息"""
        if name in self.elements:
            if "center" in kwargs:
                center = kwargs["center"]
                kwargs["x"] = center.get("x", self.elements[name].get("x"))
                kwargs["y"] = center.get("y", self.elements[name].get("y"))
            self.elements[name].update(kwargs)

    def remove_element(self, name: str):
        """删除元素"""
        if name in self.elements:
            del self.elements[name]

    def count(self) -> int:
        """获取元素数量"""
        return len(self.elements)

    def clear(self):
        """清空元素库"""
        self.elements.clear()

    def save_to_file(self, config_file: str = None):
        """保存元素库到 JSON 文件"""
        if config_file is None:
            config_file = self.config_file
        from pathlib import Path
        Path(config_file).parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(self.elements, f, ensure_ascii=False, indent=2)
        print(f"[OK] UI 元素库已保存到：{config_file}")

    def load_from_file(self, config_file: str):
        """从 JSON 文件加载元素库"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                loaded_elements = json.load(f)
            for name, info in loaded_elements.items():
                if name not in self.elements:
                    self.elements[name] = info
            print(f"[OK] 已从 {config_file} 加载 {len(loaded_elements)} 个 UI 元素")
        except Exception as e:
            print(f"[WARN] 加载配置文件失败：{e}")
            print("[INFO] 将使用默认配置")


# ==================== 全局单例 ====================

_global_library = None


def get_element_library() -> ElementLibrary:
    """获取全局 UI 元素库实例（每次调用重新从文件加载，保证数据最新）"""
    global _global_library
    if _global_library is None:
        _global_library = ElementLibrary()
    else:
        # 重新从文件加载，防止文件被外部修改后单例持有旧数据
        _global_library.elements.clear()
        from pathlib import Path
        if Path(_global_library.config_file).exists():
            _global_library.load_from_file(_global_library.config_file)
    return _global_library

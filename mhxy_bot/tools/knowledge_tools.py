# -*- coding: utf-8 -*-
"""Game knowledge-base tools for OmniMHXY."""
from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool

_TEMPLATE_FILE = "_模板.md"


def make_knowledge_tools(knowledge_dir: Path) -> list:
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    @tool
    def list_game_knowledge() -> str:
        """列出所有可用的游戏知识话题。执行任务前先调用此工具。"""
        files = sorted(f for f in knowledge_dir.glob("*.md") if f.name != _TEMPLATE_FILE)
        if not files:
            return "知识库暂无内容。"
        return "可用知识话题：\n" + "\n".join(f"- {f.stem}" for f in files)

    @tool
    def get_game_knowledge(topic: str) -> str:
        """读取指定话题的游戏操作知识。topic 为话题名称，不含 .md 后缀。"""
        path = knowledge_dir / f"{topic}.md"
        if not path.exists():
            available = [f.stem for f in knowledge_dir.glob("*.md") if f.name != _TEMPLATE_FILE]
            hint = f"可用话题：{available}" if available else "知识库暂无内容"
            return f"未找到话题「{topic}」。{hint}"
        return path.read_text(encoding="utf-8")

    @tool
    def get_knowledge_template() -> str:
        """获取知识库标准写作模板。保存新知识前应先调用。"""
        template_path = knowledge_dir / _TEMPLATE_FILE
        if not template_path.exists():
            return "模板文件不存在。"
        return template_path.read_text(encoding="utf-8")

    @tool
    def save_game_knowledge(topic: str, content: str) -> str:
        """将总结好的游戏操作知识保存到知识库。"""
        if not topic or not topic.strip():
            return "错误：话题名称不能为空。"
        if topic.startswith("_"):
            return "错误：话题名称不能以下划线开头。"

        safe_topic = topic.strip().replace("/", "").replace("\\", "").replace("..", "")
        path = knowledge_dir / f"{safe_topic}.md"
        is_update = path.exists()
        path.write_text(content, encoding="utf-8")
        action = "更新" if is_update else "新增"
        return f"已{action}知识话题「{safe_topic}」（{len(content)} 字符）。"

    return [list_game_knowledge, get_game_knowledge, get_knowledge_template, save_game_knowledge]

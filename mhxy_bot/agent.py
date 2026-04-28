# -*- coding: utf-8 -*-
"""OmniMHXY Agent assembly."""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

from core.agent_base import build_dynamic_agent
from core.model_registry import make_mhxy_registry
from core.tools.memory_tools import get_user_profile, make_memory_tools
from core.vl_model_registry import VlModelConfig, VlModelRegistry
from mhxy_bot.config import CONFIG_DIR, DATA_DIR, INSTANCES_JSON
from mhxy_bot.tools.game_tools import make_game_tools
from mhxy_bot.tools.knowledge_tools import make_knowledge_tools

load_dotenv()

SANDBOX_DIR = (DATA_DIR / "agent_workspace").resolve()
MEMORY_DIR = (DATA_DIR / "memory").resolve()
KNOWLEDGE_DIR = (DATA_DIR / "knowledge_base").resolve()
SETTINGS_DIR = DATA_DIR.resolve()

for _dir in [SANDBOX_DIR, MEMORY_DIR, KNOWLEDGE_DIR, SETTINGS_DIR, CONFIG_DIR]:
    _dir.mkdir(parents=True, exist_ok=True)

registry = make_mhxy_registry(SETTINGS_DIR)

vl_registry = VlModelRegistry(
    configs=[
        VlModelConfig(key="plus", display_name="Qwen3-VL Plus", model="qwen3-vl-plus"),
        VlModelConfig(key="flash", display_name="Qwen3-VL Flash", model="qwen3-vl-flash"),
    ],
    settings_path=SETTINGS_DIR / "vl_model_settings.json",
    default_key="plus",
)


GAME_SYSTEM_PROMPT = """你是梦幻西游手游的智能控制助理，通过 ADB 远程操控 Windows MuMu 模拟器。

当前时间：{current_time}
实例状态：
{user_profile}

## 思维链输出要求
在回复前，先用 `<think>` 标签输出你的思考过程，便于观测平台记录：
<think>
1. 分析用户意图
2. 确定需要调用的工具
3. 规划执行步骤
</think>

然后给出最终回复。

## 你的能力
- 感知：截图（capture_screenshot）、截图+OCR（sense_screen）
- 点击：按坐标点击（tap_coordinate）；批量点击所有实例同一坐标（batch_tap_coordinate，ports 留空=全部实例）；按元素库名称点击（tap_saved_element）
- 按键：返回键（press_back）；批量返回（batch_press_back，ports 留空=全部实例）
- 场景分析：用视觉模型理解当前界面（analyze_scene）
- 元素定位：用 Qwen-VL 定位指定 UI 元素坐标（locate_element_vl）
- 元素库：列出已保存元素（list_element_library）、保存元素坐标（save_to_element_library）、删除失效元素（delete_from_element_library）
- 管理：查看所有实例（get_instances）、批量识别门派（batch_recognize_schools）
- 记忆：记录关键信息（update_user_memory）

## 坐标系统
- 所有坐标均为归一化值（0-1），左上角为 (0,0)，右下角为 (1,1)
- 分辨率为 1600×900，tap_coordinate 会自动转换并加随机偏移

## 操作准则
1. 执行点击前必须先调用 list_element_library；库中有匹配元素则优先复用坐标，库中没有再用 OCR / VL。
2. 通过 sense_screen 或 locate_element_vl 找到可复用固定按钮，且点击验证成功后，应调用 save_to_element_library 保存。
3. 点击已保存元素后验证失败，应删除旧元素，再重新识别、验证、保存。
4. 点击前先感知；操作后用 sense_screen 或 analyze_scene 验证结果。
5. 批量操作多个实例时，逐个顺序执行，不要并发。
6. 遇到错误及时告知用户，不要静默重试超过 2 次。

## 端口格式
端口可以是纯数字（如 5557）或完整格式（如 127.0.0.1:5557），工具会自动处理。

## 游戏知识库
执行任务前应先查阅：
- list_game_knowledge：列出所有可用话题
- get_game_knowledge(topic)：读取指定话题的详细操作知识

执行策略：
1. 接到任务时，先查 list_game_knowledge 确认是否有对应知识。
2. 有知识时按指南执行；没有知识时逐步探索。
3. 完成新任务后，调用 get_knowledge_template 并用 save_game_knowledge 写入经验。

## 知识采集模式
当用户说“帮我创建 XX 知识”“我来教你 XX 怎么做”时，按模板逐节提问，每次只问 1-2 个问题，确认后保存。
"""


def get_instances_summary() -> str:
    """Read configured emulator instances for injection into user_profile."""
    if not INSTANCES_JSON.exists():
        return "（实例信息未初始化，请先配置 data/mhxy/config/instances.json）"
    try:
        data = json.loads(INSTANCES_JSON.read_text(encoding="utf-8"))
        lines = []
        for inst in data.get("instances", []):
            note = inst.get("note", "")
            note_str = f" [{note}]" if note else ""
            lines.append(f"  端口 {inst['port']} - {inst.get('school', '未识别')}{note_str}")
        groups = data.get("groups", [])
        if groups:
            lines.append("队伍：")
            for group in groups:
                leader = group.get("leader", {})
                members = "、".join(str(m.get("port")) for m in group.get("members", []))
                lines.append(f"  队长 {leader.get('port')} 带队员 {members}")
        return "\n".join(lines) if lines else "（暂无实例数据）"
    except Exception as e:
        return f"（实例信息读取失败：{type(e).__name__} - {e}）"


_tools = (
    make_game_tools(SANDBOX_DIR, vl_registry)
    + make_memory_tools(MEMORY_DIR)
    + make_knowledge_tools(KNOWLEDGE_DIR)
)

agent_with_chat_history = build_dynamic_agent(
    registry=registry,
    system_prompt=GAME_SYSTEM_PROMPT,
    tools=_tools,
    memory_dir=MEMORY_DIR,
)


def get_user_profile_fn() -> str:
    """Merge long-term user memory with live instance config."""
    profile = get_user_profile(MEMORY_DIR)
    instances = get_instances_summary()
    return f"{profile}\n\n【模拟器实例】\n{instances}"

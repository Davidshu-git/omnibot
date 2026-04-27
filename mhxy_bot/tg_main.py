# -*- coding: utf-8 -*-
"""OmniMHXY Telegram entrypoint."""
from __future__ import annotations

import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from core.logging_setup import setup_logging
from core.model_switch_handler import _build_model_keyboard, _build_model_text, register_model_switch
from core.tg_base import TelegramBotBase
from mhxy_bot.agent import (
    SANDBOX_DIR,
    agent_with_chat_history,
    get_user_profile_fn,
    registry,
)
from mhxy_bot.game_core.cloud_vision import set_log_callback

load_dotenv()

OBS_DIR = (Path(__file__).parent.parent / "data" / "mhxy" / "observability" / "sessions").resolve()
OBS_DIR.mkdir(parents=True, exist_ok=True)

setup_logging("./logs", "mhxy")
logger = logging.getLogger(__name__)

TG_BOT_TOKEN = os.getenv("MHXY_TG_BOT_TOKEN", "")
ALLOWED_TG_USERS = os.getenv("MHXY_ALLOWED_TG_USERS", "")
ALLOWED_USER_IDS: list[int] = [
    int(u.strip()) for u in ALLOWED_TG_USERS.split(",") if u.strip().isdigit()
]

if not TG_BOT_TOKEN:
    raise ValueError("🚨 致命错误：MHXY_TG_BOT_TOKEN 未配置！")
if not ALLOWED_USER_IDS:
    raise ValueError("🚨 致命错误：MHXY_ALLOWED_TG_USERS 未配置或格式错误！")


class GameBot(TelegramBotBase):
    """OmniMHXY Telegram Bot."""

    def get_bot_name(self) -> str:
        return "OmniMHXY"

    def get_bot_commands(self) -> list[BotCommand]:
        return [
            BotCommand("start", "🏠 唤醒主控台"),
            BotCommand("status", "📊 查询状态"),
            BotCommand("model", "🤖 切换 LLM 模型"),
            BotCommand("new", "🗑️ 清空对话历史"),
        ]

    def get_dashboard_keyboard(self) -> list[list[InlineKeyboardButton]]:
        return [
            [
                InlineKeyboardButton("📋 查看所有实例", callback_data="cmd_instances"),
                InlineKeyboardButton("🔍 识别所有门派", callback_data="cmd_recognize"),
            ],
            [
                InlineKeyboardButton("📸 截图当前屏幕", callback_data="cmd_screenshot"),
                InlineKeyboardButton("👁️ OCR 识别屏幕", callback_data="cmd_sense"),
            ],
            [
                InlineKeyboardButton("📚 查看游戏知识", callback_data="cmd_game_kb"),
                InlineKeyboardButton("🤖 切换模型", callback_data="model_menu"),
            ],
        ]

    def get_welcome_text(self, first_name: str) -> str:
        return (
            f"<blockquote><b>🎮 OmniMHXY 已挂载</b></blockquote>\n"
            f"指挥官 <b>{first_name}</b>，连接安全。\n\n"
            f"<i>可直接输入自然语言下达指令，例如："
            f"「截图端口 5557 看看当前界面」「识别所有实例的门派」。</i>"
        )

    def get_extra_status_text(self) -> str:
        cfg = registry.current()
        return f"🤖 当前模型：<b>{cfg.display_name}</b>"

    def get_tool_status_map(self) -> dict[str, str]:
        return {
            "get_instances": "📋 正在读取模拟器实例配置...",
            "batch_recognize_schools": "🏯 正在批量识别所有实例门派，请耐心等待...",
            "capture_screenshot": "📸 正在截取模拟器屏幕...",
            "sense_screen": "👁️ 正在截图并 OCR 识别屏幕...",
            "analyze_scene": "🔍 正在调用视觉模型分析游戏场景...",
            "locate_element_vl": "🔍 正在调用 Qwen-VL 定位 UI 元素...",
            "tap_coordinate": "👆 正在执行点击操作...",
            "batch_tap_coordinate": "👆 正在批量执行点击操作...",
            "tap_saved_element": "👆 正在点击已保存 UI 元素...",
            "press_back": "↩️ 正在按返回键...",
            "batch_press_back": "↩️ 正在批量按返回键...",
            "list_element_library": "📚 正在读取 UI 元素库...",
            "save_to_element_library": "💾 正在保存元素到 UI 元素库...",
            "delete_from_element_library": "🗑️ 正在删除失效 UI 元素...",
            "list_game_knowledge": "📚 正在读取游戏知识库目录...",
            "get_game_knowledge": "📖 正在读取游戏操作知识...",
            "save_game_knowledge": "💾 正在保存游戏操作知识...",
        }

    def set_observability_context(self, observer) -> None:
        set_log_callback(observer)

    async def setup_extra_handlers(self, app: Application) -> None:
        app.add_handler(CommandHandler("new", self._new_command))
        register_model_switch(app, registry)

    async def handle_custom_cmd(
        self,
        cmd: str,
        query,
        user_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        update: Update,
    ) -> str:
        if cmd == "cmd_instances":
            return "查看所有模拟器实例的信息，包括端口、门派和队伍配置。"
        if cmd == "cmd_recognize":
            return "批量识别所有模拟器实例的门派，并更新实例配置。"
        if cmd == "cmd_screenshot":
            return "请先查看实例列表。如果用户没有指定端口，就询问要截图哪个端口；如果能判断默认端口，则调用 capture_screenshot 截图。"
        if cmd == "cmd_sense":
            return "请先查看实例列表。如果用户没有指定端口，就询问要 OCR 识别哪个端口；如果能判断默认端口，则调用 sense_screen 识别。"
        if cmd == "cmd_game_kb":
            return "列出游戏知识库中现在有哪些话题可以读取。"
        if cmd == "model_menu":
            if query.message:
                await query.message.reply_text(
                    _build_model_text(registry),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_build_model_keyboard(registry),
                )
            return ""
        logger.warning(f"未知按钮指令：{cmd}")
        return ""

    async def _new_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return
        memory_file = Path("data/mhxy/memory") / f"tg_session_{user.id}.json"
        memory_file.unlink(missing_ok=True)
        await message.reply_text(
            "<blockquote><b>🗑️ 对话历史已清空</b></blockquote>\n可以开始新的游戏控制会话。",
            parse_mode=ParseMode.HTML,
        )


def main() -> None:
    bot = GameBot(
        bot_token=TG_BOT_TOKEN,
        allowed_user_ids=ALLOWED_USER_IDS,
        agent=agent_with_chat_history,
        get_user_profile_fn=get_user_profile_fn,
        sandbox_dir=SANDBOX_DIR,
        kb_dir=None,
        job_module=None,
        obs_dir=OBS_DIR,
        agent_id="mhxy-bot",
    )
    bot.run()


if __name__ == "__main__":
    main()

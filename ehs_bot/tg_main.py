"""
EHS Bot Telegram 入口 - 继承 TelegramBotBase，注入 EHS 领域专属逻辑。
"""
import os
import logging
from pathlib import Path

from core.logging_setup import setup_logging

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from core.tg_base import TelegramBotBase
from core.model_switch_handler import register_model_switch
from ehs_bot.agent import (
    agent_with_chat_history,
    get_user_profile_fn,
    registry,
    SANDBOX_DIR,
    KB_DIR,
)

OBS_DIR = (Path(__file__).parent.parent / "data" / "ehs" / "observability" / "sessions").resolve()
OBS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv()

setup_logging("./logs", "ehs")
logger = logging.getLogger(__name__)

TG_BOT_TOKEN = os.getenv("EHS_TG_BOT_TOKEN", "")
ALLOWED_TG_USERS = os.getenv("EHS_ALLOWED_TG_USERS", "")
ALLOWED_USER_IDS: list[int] = [
    int(u.strip()) for u in ALLOWED_TG_USERS.split(",") if u.strip().isdigit()
]

if not TG_BOT_TOKEN:
    raise ValueError("🚨 致命错误：EHS_TG_BOT_TOKEN 未配置！")
if not ALLOWED_USER_IDS:
    raise ValueError("🚨 致命错误：EHS_ALLOWED_TG_USERS 未配置或格式错误（需为逗号分隔的整数用户 ID）！")


class EHSBot(TelegramBotBase):
    """OmniEHS Telegram Bot — EHS 专业知识顾问实例。"""

    def get_bot_name(self) -> str:
        return "OmniEHS"

    def get_bot_commands(self) -> list[BotCommand]:
        return [
            BotCommand("start", "🏠 唤醒主控台"),
            BotCommand("kb", "📚 调阅知识库档案"),
            BotCommand("kb_cleanup", "🗑️ 清理知识库"),
            BotCommand("report", "📝 触发 EHS 定期简报"),
            BotCommand("status", "📊 查询最新任务进度"),
            BotCommand("model", "🤖 切换 LLM 模型"),
        ]

    def get_dashboard_keyboard(self) -> list[list[InlineKeyboardButton]]:
        return [
            [InlineKeyboardButton("📚 调阅知识库档案", callback_data="cmd_kb_list")],
            [InlineKeyboardButton("🗑️ 清理知识库", callback_data="cmd_kb_cleanup")],
            [InlineKeyboardButton("📝 触发 EHS 定期简报", callback_data="cmd_trigger_job")],
            [InlineKeyboardButton("📊 查询最新任务进度", callback_data="cmd_status")],
        ]

    def get_welcome_text(self, first_name: str) -> str:
        return (
            f"<blockquote><b>🛡️ OmniEHS 专业顾问已上线</b></blockquote>\n"
            f"您好 <b>{first_name}</b>，连接安全。\n\n"
            f"<i>您可以直接用自然语言咨询 EHS 专业问题，或通过下方面板快速执行任务：</i>"
        )

    def get_extra_status_text(self) -> str:
        cfg = registry.current()
        return f"🤖 当前模型：<b>{cfg.display_name}</b>"

    def get_tool_status_map(self) -> dict[str, str]:
        return {
            "trigger_job": "🚀 正在将简报任务投递至独立进程...",
            "preview_kb_cleanup": "🔍 正在扫描知识库文件列表...",
            "execute_kb_cleanup": "🗑️ 正在清理知识库文件及向量缓存...",
        }

    async def setup_extra_handlers(self, app: Application) -> None:
        from telegram.ext import CommandHandler
        app.add_handler(CommandHandler("kb", self._kb_command))
        app.add_handler(CommandHandler("kb_cleanup", self._kb_cleanup_command))
        app.add_handler(CommandHandler("report", self._report_command))
        register_model_switch(app, registry)

    async def handle_custom_cmd(
        self,
        cmd: str,
        query,
        user_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        update: Update,
    ) -> str:
        if cmd == "cmd_kb_list":
            return "列出知识库里现在有哪些文件可以读取？"
        if cmd == "cmd_kb_cleanup":
            return "请先用 preview_kb_cleanup 扫描整个知识库，列出所有文件（含大小和存放时长），然后询问我想删除哪些。"
        logger.warning(f"未知按钮指令：{cmd}")
        return ""

    async def _kb_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        import uuid
        from datetime import datetime
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return
        await message.reply_text(
            "<blockquote><b>⚡ 菜单指令注入：</b>\n<i>查看知识库文件</i></blockquote>",
            parse_mode=ParseMode.HTML,
        )
        await self.execute_agent_task(
            "列出知识库里现在有哪些文件可以读取？",
            message, user.id, context, update,
        )

    async def _kb_cleanup_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return
        await message.reply_text(
            "<blockquote><b>⚡ 菜单指令注入：</b>\n<i>清理知识库</i></blockquote>",
            parse_mode=ParseMode.HTML,
        )
        await self.execute_agent_task(
            "请先用 preview_kb_cleanup 扫描整个知识库，列出所有文件（含大小和存放时长），然后询问我想删除哪些。",
            message, user.id, context, update,
        )

    async def _report_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        import uuid
        from datetime import datetime
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return
        try:
            job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            await self._dispatch_job_task(message, job_id)
        except Exception as e:
            logger.error(f"/report 命令执行失败：{e}")
            await message.reply_text(f"⚠️ 任务派发失败：{type(e).__name__} - {str(e)}")


def main() -> None:
    bot = EHSBot(
        bot_token=TG_BOT_TOKEN,
        allowed_user_ids=ALLOWED_USER_IDS,
        agent=agent_with_chat_history,
        get_user_profile_fn=get_user_profile_fn,
        sandbox_dir=SANDBOX_DIR,
        kb_dir=KB_DIR,
        job_module="ehs_bot.daily_job",
        obs_dir=OBS_DIR,
        agent_id="ehs-bot",
    )
    bot.run()


if __name__ == "__main__":
    main()

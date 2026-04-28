"""
模型切换处理器 - 提供 /model 命令与 inline keyboard 切换逻辑，供两个 bot 共用。
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from core.model_registry import ModelRegistry

logger = logging.getLogger(__name__)


def register_model_switch(
    app: Application,
    registry,
    command: str = "model",
    callback_prefix: str = "switch_model",
    title: str = "🤖 LLM 模型管理",
) -> None:
    """
    向 Application 注册：
      - /{command} 命令 → 显示当前模型 + inline keyboard
      - CallbackQueryHandler(pattern=^{callback_prefix}:) → 切换并回编消息

    registry 只需实现 current() / current_key() / list_models() / switch()，
    兼容 ModelRegistry 和 VlModelRegistry。
    """

    async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None:
            return
        await message.reply_text(
            _build_model_text(registry, title),
            parse_mode=ParseMode.HTML,
            reply_markup=_build_model_keyboard(registry, callback_prefix),
        )

    async def switch_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()

        data = query.data or ""
        key = data.split(":", 1)[1] if ":" in data else ""

        try:
            registry.switch(key)
            cfg = registry.current()
            text = (
                f"<blockquote><b>✅ 模型已切换</b></blockquote>\n"
                f"当前模型：<b>{cfg.display_name}</b>\n"
                f"<i>下一条消息将使用新模型，历史记录已保留。</i>"
            )
        except ValueError as e:
            text = f"⚠️ 切换失败：{e}"

        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_build_model_keyboard(registry, callback_prefix),
            )
        except Exception as exc:
            logger.warning(f"[model_switch] edit_message_text 失败：{exc}")

    app.add_handler(CommandHandler(command, model_command))
    app.add_handler(
        CallbackQueryHandler(switch_model_callback, pattern=rf"^{callback_prefix}:"),
        group=-1,
    )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _build_model_text(registry, title: str = "🤖 LLM 模型管理") -> str:
    cfg = registry.current()
    return (
        f"<blockquote><b>{title}</b></blockquote>\n"
        f"当前模型：<b>{cfg.display_name}</b>\n\n"
        f"<i>点击下方按钮即可热切换，无需重启。</i>"
    )


def _build_model_keyboard(registry, callback_prefix: str = "switch_model") -> InlineKeyboardMarkup:
    current = registry.current_key()
    buttons = []
    for cfg in registry.list_models():
        label = f"✅ {cfg.display_name}" if cfg.key == current else cfg.display_name
        buttons.append([InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{cfg.key}")])
    return InlineKeyboardMarkup(buttons)

"""
Stock Bot Telegram 入口 - 继承 TelegramBotBase，注入股票领域专属逻辑。
"""
import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from filelock import FileLock
from telegram import Update, InlineKeyboardButton, BotCommand, Message
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from core.tg_base import TelegramBotBase
from stock_bot.agent import (
    agent_with_chat_history,
    get_user_profile_fn,
    SANDBOX_DIR,
    KB_DIR,
    MEMORY_DIR,
)
from stock_bot.valuation_engine import fetch_stock_price_raw

load_dotenv()

logger = logging.getLogger(__name__)

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
ALLOWED_TG_USERS = os.getenv("ALLOWED_TG_USERS", "")
ALLOWED_USER_IDS: list[int] = [
    int(u.strip()) for u in ALLOWED_TG_USERS.split(",") if u.strip().isdigit()
]

if not TG_BOT_TOKEN:
    raise ValueError("🚨 致命错误：TG_BOT_TOKEN 未配置！")
if not ALLOWED_USER_IDS:
    raise ValueError("🚨 致命错误：ALLOWED_TG_USERS 未配置或格式错误（需为逗号分隔的整数用户 ID）！")


class StockBot(TelegramBotBase):
    """OmniStock Telegram Bot — 股票量化分析专属实例。"""

    # ------------------------------------------------------------------
    # 钩子实现
    # ------------------------------------------------------------------

    def get_bot_name(self) -> str:
        return "OmniStock"

    def get_bot_commands(self) -> list[BotCommand]:
        return [
            BotCommand("start", "🏠 唤醒主控台"),
            BotCommand("portfolio", "💼 盘点当前持仓与盈亏"),
            BotCommand("report", "📝 极速触发盘后研报"),
            BotCommand("status", "📊 查询最新任务进度"),
            BotCommand("kb", "📚 调阅历史情报档案"),
            BotCommand("alert", "🔔 设定盯盘价格预警"),
        ]

    def get_dashboard_keyboard(self) -> list[list[InlineKeyboardButton]]:
        return [
            [InlineKeyboardButton("💼 盘点当前持仓与盈亏", callback_data="cmd_portfolio")],
            [InlineKeyboardButton("📝 极速触发盘后研报", callback_data="cmd_trigger_job")],
            [InlineKeyboardButton("🔔 设定盯盘价格预警", callback_data="cmd_alert")],
            [InlineKeyboardButton("📊 查询最新任务进度", callback_data="cmd_status")],
            [InlineKeyboardButton("📚 调阅历史情报档案", callback_data="cmd_kb_list")],
        ]

    def get_welcome_text(self, first_name: str) -> str:
        return (
            f"<blockquote><b>🚀 OmniStock 量化中台已挂载</b></blockquote>\n"
            f"指挥官 <b>{first_name}</b>，连接安全。\n\n"
            f"<i>您可以直接输入自然语言下达指令，或通过下方战术面板执行核心宏任务：</i>"
        )

    def get_tool_status_map(self) -> dict[str, str]:
        return {
            "get_universal_stock_price": "📈 正在拉取全球实时盘面数据...",
            "get_etf_price": "📊 正在拉取 ETF 基金核心数据...",
            "draw_universal_stock_chart": "🎨 正在启动绘图引擎渲染 K 线...",
            "search_company_ticker": "🔍 正在全网检索股票代码...",
            "calculate_exact_portfolio_value": "🧮 正在使用程序精确核算财务数据...",
            "create_price_alert": "🔔 正在将盯盘预警挂载到后台引擎...",
            "trigger_job": "🚀 正在将研报任务投递至独立进程...",
        }

    async def setup_job_queue(self, app: Application) -> None:
        """挂载每 5 分钟一次的纯 Python 盯盘巡检器。"""
        if app.job_queue:
            app.job_queue.run_repeating(self._price_watcher_routine, interval=300, first=10)
            logger.info("✅ 盯盘价格预警定时任务已挂载（每 5 分钟）")

    async def setup_extra_handlers(self, app: Application) -> None:
        """注册股票专属命令处理器。"""
        from telegram.ext import CommandHandler
        app.add_handler(CommandHandler("portfolio", self._portfolio_command))
        app.add_handler(CommandHandler("report", self._report_command))
        app.add_handler(CommandHandler("kb", self._kb_command))
        app.add_handler(CommandHandler("alert", self._alert_command))

    async def handle_custom_cmd(
        self,
        cmd: str,
        query,
        user_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        update: Update,
    ) -> str:
        """处理股票专属 Inline 按钮指令。"""
        if cmd == "cmd_portfolio":
            return "帮我精确计算当前总市值和持仓盈亏，并生成财务明细报表。"

        if cmd == "cmd_kb_list":
            return "列出知识库里现在有哪些文件可以读取？"

        if cmd == "cmd_alert":
            text = (
                "<blockquote><b>🔔 智能盯盘预警系统</b></blockquote>\n"
                "您现在可以使用自然语言下发盯盘指令，AI 会自动为您解析参数并挂载到后台监控引擎。\n\n"
                "<b>您可以直接这样对我说：</b>\n"
                "🗣️ <i>「帮我盯着英伟达，如果跌破 100 块叫我一声。」</i>\n"
                "🗣️ <i>「如果腾讯涨破了 350 港币，发消息提醒我减仓。」</i>"
            )
            if query.message:
                await query.message.reply_text(text, parse_mode=ParseMode.HTML)
            return ""

        logger.warning(f"未知按钮指令：{cmd}")
        return ""

    # ------------------------------------------------------------------
    # 股票专属命令处理器
    # ------------------------------------------------------------------

    async def _portfolio_command(
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
            "<blockquote><b>⚡ 原生菜单指令注入：</b>\n<i>精确核算总市值</i></blockquote>",
            parse_mode=ParseMode.HTML,
        )
        await self.execute_agent_task(
            "帮我精确计算当前总市值和持仓盈亏，并生成财务明细报表。",
            message, user.id, context, update,
        )

    async def _report_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        import uuid
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

    async def _kb_command(
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
            "<blockquote><b>⚡ 原生菜单指令注入：</b>\n<i>查看知识库文件</i></blockquote>",
            parse_mode=ParseMode.HTML,
        )
        await self.execute_agent_task(
            "列出知识库里现在有哪些文件可以读取？",
            message, user.id, context, update,
        )

    async def _alert_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return
        text = (
            "<blockquote><b>🔔 智能盯盘预警系统</b></blockquote>\n"
            "您现在可以使用自然语言下发盯盘指令，AI 会自动为您解析参数并挂载到后台监控引擎。\n\n"
            "<b>您可以直接这样对我说：</b>\n"
            "🗣️ <i>「帮我盯着英伟达，如果跌破 100 块叫我一声。」</i>\n"
            "🗣️ <i>「如果腾讯涨破了 350 港币，发消息提醒我减仓。」</i>"
        )
        await message.reply_text(text, parse_mode=ParseMode.HTML)

    # ------------------------------------------------------------------
    # 价格预警定时任务
    # ------------------------------------------------------------------

    async def _price_watcher_routine(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """纯 Python 轻量级盯盘引擎（每 5 分钟执行，0 Token 消耗）。"""
        alerts_file = MEMORY_DIR / "alerts.json"
        alerts_lock = MEMORY_DIR / "alerts.json.lock"

        if not alerts_file.exists():
            return

        try:
            with FileLock(alerts_lock, timeout=3):
                with open(alerts_file, 'r', encoding='utf-8') as f:
                    alerts = json.load(f)
        except Exception:
            return

        if not alerts:
            return

        changed = False

        for chat_id_str, user_tasks in list(alerts.items()):
            chat_id = int(chat_id_str)
            triggered_keys: list[str] = []

            for task_key, task_info in user_tasks.items():
                ticker = task_info['ticker']
                operator = task_info['operator']
                target_price = float(task_info['target_price'])

                try:
                    price_data = fetch_stock_price_raw(ticker)
                    current_price = float(price_data.get('close', 0))
                    if current_price == 0:
                        continue

                    is_triggered = (
                        (operator == '<' and current_price <= target_price)
                        or (operator == '>' and current_price >= target_price)
                    )

                    if is_triggered:
                        alert_msg = (
                            f"<blockquote><b>🚨 智能盯盘触发警告</b></blockquote>\n"
                            f"标的代码：<b>{ticker}</b>\n"
                            f"预警条件：{operator} {target_price}\n"
                            f"当前最新价：<b>{current_price}</b>\n\n"
                            f"<i>系统已自动将此单次预警任务销毁。</i>"
                        )
                        await context.bot.send_message(
                            chat_id=chat_id, text=alert_msg, parse_mode=ParseMode.HTML
                        )
                        triggered_keys.append(task_key)

                except Exception as e:
                    logger.warning(f"预警查价失败 {ticker}: {e}")

            for key in triggered_keys:
                del alerts[chat_id_str][key]
                changed = True

        if changed:
            try:
                with FileLock(alerts_lock, timeout=3):
                    with open(alerts_file, 'w', encoding='utf-8') as f:
                        json.dump(alerts, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"预警文件回写失败：{e}")


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    bot = StockBot(
        bot_token=TG_BOT_TOKEN,
        allowed_user_ids=ALLOWED_USER_IDS,
        agent=agent_with_chat_history,
        get_user_profile_fn=get_user_profile_fn,
        sandbox_dir=SANDBOX_DIR,
        kb_dir=KB_DIR,
        job_module="stock_bot.daily_job",
    )
    bot.run()


if __name__ == "__main__":
    main()

"""
Stock Bot Telegram 入口 - 继承 TelegramBotBase，注入股票领域专属逻辑。
"""
import os
import json
import asyncio
import logging
from pathlib import Path

from core.logging_setup import setup_logging
from datetime import datetime

from dotenv import load_dotenv
from filelock import FileLock
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from core.tg_base import TelegramBotBase
from core.model_switch_handler import register_model_switch
from stock_bot.agent import (
    agent_with_chat_history,
    get_user_profile_fn,
    registry,
    SANDBOX_DIR,
    KB_DIR,
    MEMORY_DIR,
)
from stock_bot.valuation_engine import fetch_stock_price_raw

OBS_DIR = (Path(__file__).parent.parent / "data" / "stock" / "observability" / "sessions").resolve()
OBS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv()

setup_logging("./logs", "stock")
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
            BotCommand("status", "📊 查询最新任务进度"),
            BotCommand("model", "🤖 切换 LLM 模型"),
        ]

    def get_dashboard_keyboard(self) -> list[list[InlineKeyboardButton]]:
        return [
            [
                InlineKeyboardButton("💼 盘点当前持仓盈亏", callback_data="cmd_portfolio"),
                InlineKeyboardButton("📰 快查全球市场资讯", callback_data="cmd_market_news"),
            ],
            [
                InlineKeyboardButton("📝 极速触发盘后研报", callback_data="cmd_trigger_job"),
                InlineKeyboardButton("📊 查询最新任务进度", callback_data="cmd_status"),
            ],
            [
                InlineKeyboardButton("🔔 设定盯盘价格预警", callback_data="cmd_alert"),
                InlineKeyboardButton("📋 查看管理盯盘预警", callback_data="cmd_alert_manage"),
            ],
            [
                InlineKeyboardButton("📚 调阅历史情报档案", callback_data="cmd_kb_list"),
                InlineKeyboardButton("🗑️ 清理盘点知识档案", callback_data="cmd_kb_cleanup"),
            ],
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
            "list_price_alerts": "📋 正在读取当前盯盘预警列表...",
            "delete_price_alert": "🗑️ 正在删除指定盯盘预警...",
            "trigger_job": "🚀 正在将研报任务投递至独立进程...",
            "preview_kb_cleanup": "🔍 正在扫描知识库文件列表...",
            "execute_kb_cleanup": "🗑️ 正在清理知识库文件及向量缓存...",
            "fetch_market_news": "📰 正在多源聚合最新全球市场资讯...",
        }

    async def setup_job_queue(self, app: Application) -> None:
        """挂载每 5 分钟一次的纯 Python 盯盘巡检器。"""
        if app.job_queue:
            app.job_queue.run_repeating(self._price_watcher_routine, interval=60, first=10)
            logger.info("✅ 盯盘价格预警定时任务已挂载（每 5 分钟）")

    async def setup_extra_handlers(self, app: Application) -> None:
        """注册股票专属命令处理器。"""
        from telegram.ext import CallbackQueryHandler
        app.add_handler(CallbackQueryHandler(self._delete_alert_callback, pattern=r"^del_alert:"), group=-1)
        register_model_switch(app, registry)

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

        if cmd == "cmd_market_news":
            return "帮我快速汇总一下当前最新的市场资讯动态，包括 A 股、港股、美股的重要新闻和市场情绪，简明扼要。"

        if cmd == "cmd_kb_list":
            return "列出知识库里现在有哪些文件可以读取？"

        if cmd == "cmd_kb_cleanup":
            return "请先用 preview_kb_cleanup 扫描整个知识库，列出所有文件（含大小和存放时长），然后询问我想删除哪些。"

        if cmd == "cmd_alert":
            text = (
                "<b>🔔 智能盯盘预警系统</b>\n"
                "您现在可以使用自然语言下发盯盘指令，AI 会自动为您解析参数并挂载到后台监控引擎。\n\n"
                "<b>您可以直接这样对我说：</b>\n"
                "🗣️ <i>「帮我盯着英伟达，如果跌破 100 块叫我一声。」</i>\n"
                "🗣️ <i>「如果腾讯涨破了 350 港币，发消息提醒我减仓。」</i>"
            )
            if query.message:
                await query.message.reply_text(text, parse_mode=ParseMode.HTML)
            return ""

        if cmd == "cmd_alert_manage":
            return "查看我当前所有的盯盘预警，如果有预警就列出来，并询问我是否需要删除其中某条。"

        logger.warning(f"未知按钮指令：{cmd}")
        return ""


    async def _delete_alert_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """处理预警推送消息上的【删除此预警】按钮，纯 Python 直接操作，不走 LLM。"""
        query = update.callback_query
        if query is None:
            return
        await query.answer()

        parts = (query.data or "").split(":", 2)
        if len(parts) != 3:
            return
        _, chat_id_str, task_key = parts

        alerts_file = MEMORY_DIR / "alerts.json"
        alerts_lock = MEMORY_DIR / "alerts.json.lock"

        try:
            with FileLock(alerts_lock, timeout=5):
                with open(alerts_file, 'r', encoding='utf-8') as f:
                    alerts = json.load(f)

                user_alerts = alerts.get(chat_id_str, {})
                if task_key not in user_alerts:
                    await query.answer("该预警已不存在。", show_alert=True)
                    return

                del user_alerts[task_key]
                alerts[chat_id_str] = user_alerts

                with open(alerts_file, 'w', encoding='utf-8') as f:
                    json.dump(alerts, f, ensure_ascii=False, indent=2)

            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ 预警已删除。")

        except Exception as e:
            logger.error(f"删除预警回调失败：{e}")
            await query.answer("删除失败，请稍后重试。", show_alert=True)

    # ------------------------------------------------------------------
    # 价格预警定时任务
    # ------------------------------------------------------------------

    _COOLDOWN_MINUTES: int = 60

    async def _price_watcher_routine(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """纯 Python 轻量级盯盘引擎（每 5 分钟执行，0 Token 消耗）。触发后进入冷却期持续监控。"""
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

        for chat_id_str, user_tasks in alerts.items():
            chat_id = int(chat_id_str)

            for task_key, task_info in user_tasks.items():
                ticker = task_info['ticker']
                operator = task_info['operator']
                target_price = float(task_info['target_price'])
                last_triggered_at = task_info.get('last_triggered_at')
                cooldown_minutes = self._COOLDOWN_MINUTES

                try:
                    # 冷却期内跳过
                    if last_triggered_at:
                        elapsed = (datetime.now() - datetime.fromisoformat(last_triggered_at)).total_seconds() / 60
                        if elapsed < cooldown_minutes:
                            continue

                    price_data = fetch_stock_price_raw(ticker)
                    current_price = float(price_data.get('close', 0))
                    if current_price == 0:
                        continue

                    is_triggered = (
                        (operator == '<' and current_price <= target_price)
                        or (operator == '>' and current_price >= target_price)
                    )

                    if is_triggered:
                        op_display = "&lt;" if operator == "<" else "&gt;"
                        alert_msg = (
                            f"<b>🚨 智能盯盘触发警告</b>\n"
                            f"标的代码：<b>{ticker}</b>\n"
                            f"预警条件：{op_display} {target_price}\n"
                            f"当前最新价：<b>{current_price}</b>\n\n"
                            f"<i>冷却 {cooldown_minutes} 分钟后将继续监控。</i>"
                        )
                        delete_btn = InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "🗑️ 删除此预警",
                                callback_data=f"del_alert:{chat_id_str}:{task_key}"
                            )
                        ]])
                        await context.bot.send_message(
                            chat_id=chat_id, text=alert_msg,
                            parse_mode=ParseMode.HTML, reply_markup=delete_btn
                        )
                        task_info['last_triggered_at'] = datetime.now().isoformat()
                        changed = True

                except Exception as e:
                    logger.warning(f"预警查价失败 {ticker}: {e}")

        if changed:
            try:
                with FileLock(alerts_lock, timeout=3):
                    with open(alerts_file, 'w', encoding='utf-8') as f:
                        json.dump(alerts, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"预警文件回写失败：{e}")


def main() -> None:

    bot = StockBot(
        bot_token=TG_BOT_TOKEN,
        allowed_user_ids=ALLOWED_USER_IDS,
        agent=agent_with_chat_history,
        get_user_profile_fn=get_user_profile_fn,
        sandbox_dir=SANDBOX_DIR,
        kb_dir=KB_DIR,
        job_module="stock_bot.daily_job",
        asr_api_key=os.getenv("GROQ_API_KEY", ""),
        obs_dir=OBS_DIR,
        agent_id="stock-bot",
    )
    bot.run()


if __name__ == "__main__":
    main()

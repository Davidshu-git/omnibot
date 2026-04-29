# -*- coding: utf-8 -*-
"""OmniMHXY Telegram entrypoint."""
from __future__ import annotations

import asyncio
import json
import os
import logging
import threading
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from core.logging_setup import setup_logging
from core.model_switch_handler import _build_model_keyboard, _build_model_text, register_model_switch
from core.tg_base import TelegramBotBase
from mhxy_bot.agent import (
    MEMORY_DIR,
    SANDBOX_DIR,
    agent_with_chat_history,
    get_user_profile_fn,
    registry,
    vl_registry,
)
from mhxy_bot.game_core.cloud_vision import set_log_callback

load_dotenv()

OBS_DIR = (Path(__file__).parent.parent / "data" / "mhxy" / "observability" / "sessions").resolve()
OBS_DIR.mkdir(parents=True, exist_ok=True)

_TASKS_DIR = Path(__file__).parent / "tasks"
_INSTANCES_PATH = Path(__file__).parent.parent / "data" / "mhxy" / "config" / "instances.json"

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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_task_results: list[dict] = []
        self._task_running: bool = False
        self._active_ctxs: list = []
        self._active_ctxs_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Task state helpers
    # ------------------------------------------------------------------

    def _register_ctx(self, ctx) -> None:
        with self._active_ctxs_lock:
            self._active_ctxs.append(ctx)

    def _unregister_ctx(self, ctx) -> None:
        with self._active_ctxs_lock:
            try:
                self._active_ctxs.remove(ctx)
            except ValueError:
                pass

    def _request_stop(self) -> int:
        with self._active_ctxs_lock:
            count = len(self._active_ctxs)
            for c in self._active_ctxs:
                c.stop_requested = True
            return count

    def _make_task_observer(self, user_id: int):
        """Observer using the same session file as the regular agent messages."""
        if self.obs_dir is None or not self.agent_id:
            return None
        from core.observability import OmniObserver
        agent_slug = self.agent_id.replace("-", "_")
        today = date.today().strftime("%Y%m%d")
        obs_session_id = f"tg_session_{agent_slug}_{user_id}_{today}"
        return OmniObserver(obs_session_id, self.agent_id, self.obs_dir)

    # ------------------------------------------------------------------
    # Synchronous runner (called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _run_task_sync(
        self,
        ports: list[str],
        task_def,
        max_rounds: int,
        observer,
    ) -> list[dict]:
        """Execute task for all ports sequentially. Blocks — must run in thread."""
        from mhxy_bot.runner.task_loader import build_context, make_executor
        executor = make_executor()
        all_results: list[dict] = []

        for port in ports:
            ctx = build_context(port, executor, observer=observer)
            self._register_ctx(ctx)
            try:
                port_results = self._run_port_sync(ctx, port, task_def, max_rounds)
                all_results.extend(port_results)
            finally:
                self._unregister_ctx(ctx)
            if ctx.stop_requested:
                break

        return all_results

    def _run_port_sync(self, ctx, port: str, task_def, max_rounds: int) -> list[dict]:
        """Run max_rounds for one port; TaskEngine owns task preflight."""
        from mhxy_bot.runner.engine import TaskEngine
        from mhxy_bot.runner.models import TaskStatus

        # main rounds
        engine = TaskEngine(ctx)
        results: list[dict] = []

        for rnd in range(1, max_rounds + 1):
            ctx.info("port=%s  round %d/%d  starting", port, rnd, max_rounds)
            result = engine.run(task_def)

            entry: dict = {"port": port, "round": rnd, "status": result.status.value}
            if result.failed_step:
                entry["failed_step"] = result.failed_step
            if result.message:
                entry["message"] = result.message
            results.append(entry)

            if result.status in (TaskStatus.NEEDS_HUMAN, TaskStatus.FAILED, TaskStatus.STOPPED):
                break

        return results

    # ------------------------------------------------------------------
    # Result formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_task_results(results: list[dict]) -> str:
        if not results:
            return "⚠️ 没有可执行的实例。"
        lines: list[str] = []
        for r in results:
            port = r.get("port", "?")
            status = r.get("status", "?")
            rnd = r.get("round", 0)
            msg = r.get("message", "")
            step = r.get("failed_step", "")

            if status == "skipped":
                reason = r.get("reason", "")
                lines.append(f"⚠️ <b>port {port}</b> — 跳过（{reason}）")
            elif status == "completed":
                lines.append(f"✅ <b>port {port}</b> 第 {rnd} 轮 — 完成")
            elif status == "needs_human":
                if "denylist" in msg:
                    lines.append(
                        f"🚨 <b>port {port}</b> 第 {rnd} 轮 — "
                        f"<b>触发敏感界面，需人工确认！</b>\n"
                        f"   <code>{msg}</code>"
                    )
                else:
                    lines.append(
                        f"👨‍🔧 <b>port {port}</b> 第 {rnd} 轮 — 需要人工介入\n"
                        f"   <code>{msg}</code>"
                    )
            elif status == "failed":
                lines.append(
                    f"❌ <b>port {port}</b> 第 {rnd} 轮 — "
                    f"失败（step={step}）\n"
                    f"   <code>{msg}</code>"
                )
            elif status == "stopped":
                lines.append(f"🛑 <b>port {port}</b> 第 {rnd} 轮 — 已停止")
            else:
                lines.append(f"<b>port {port}</b> 第 {rnd} 轮 — {status}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Task command handlers
    # ------------------------------------------------------------------

    async def tasks_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权")
            return

        tasks: list[dict] = []
        for f in sorted(_TASKS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                tasks.append({
                    "id": data.get("id", f.stem),
                    "name": data.get("name", f.stem),
                    "mode": data.get("mode", "single"),
                    "max_rounds": data.get("max_rounds", 3),
                })
            except Exception:
                tasks.append({"id": f.stem, "name": f.stem, "mode": "?", "max_rounds": "?"})

        if not tasks:
            await message.reply_text("暂无可用任务。")
            return

        lines = ["<b>🎮 可用任务列表</b>\n"]
        for t in tasks:
            lines.append(
                f"• <code>{t['id']}</code> — {t['name']}  "
                f"（{t['mode']}，最多 {t['max_rounds']} 轮）"
            )
        lines.append("\n用法：<code>/run_mijing</code> 或 <code>/run_mijing 5557</code>")
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def run_mijing_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权")
            return

        if self._task_running:
            await message.reply_text(
                "⚠️ 已有任务正在执行，请等待完成或使用 /stop_task 停止。"
            )
            return

        args = context.args or []
        port = args[0].strip() if args else None

        task_path = _TASKS_DIR / "mijing.json"
        if not task_path.exists():
            await message.reply_text("❌ 找不到 mhxy_bot/tasks/mijing.json")
            return

        from mhxy_bot.runner.models import TaskDefinition
        from mhxy_bot.runner.task_loader import get_all_ports
        task_def = TaskDefinition.load(task_path)
        max_rounds = int(task_def.meta.get("max_rounds", 3))
        ports = [port] if port else get_all_ports(_INSTANCES_PATH)
        port_desc = f"port {port}" if port else f"全部 {len(ports)} 个实例"

        observer = self._make_task_observer(user.id)
        await message.reply_text(
            f"🚀 <b>秘境降妖</b> 开始执行\n"
            f"目标：{port_desc}，每实例最多 {max_rounds} 轮",
            parse_mode=ParseMode.HTML,
        )

        self._task_running = True
        try:
            results = await asyncio.to_thread(
                self._run_task_sync, ports, task_def, max_rounds, observer
            )
        finally:
            self._task_running = False

        self._last_task_results = results

        summary = self._format_task_results(results)
        has_denylist = any("denylist" in r.get("message", "") for r in results)
        header = (
            "🚨 <b>秘境降妖 — 触发敏感界面，需要人工确认！</b>"
            if has_denylist
            else "📋 <b>秘境降妖 执行完毕</b>"
        )
        await message.reply_text(
            f"{header}\n\n{summary}",
            parse_mode=ParseMode.HTML,
        )

    async def task_status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权")
            return

        if self._task_running:
            await message.reply_text("⏳ 任务正在执行中...")
            return

        if not self._last_task_results:
            await message.reply_text("暂无任务记录，请先执行 /run_mijing。")
            return

        summary = self._format_task_results(self._last_task_results)
        await message.reply_text(
            f"<b>📊 最近一次任务结果</b>\n\n{summary}",
            parse_mode=ParseMode.HTML,
        )

    async def stop_task_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权")
            return

        count = self._request_stop()
        if count == 0:
            await message.reply_text("当前没有正在执行的任务。")
        else:
            await message.reply_text(
                f"🛑 已发送停止信号（{count} 个活跃上下文），"
                f"将在当前步骤完成后停止。"
            )

    # ------------------------------------------------------------------
    # Existing overrides — unchanged
    # ------------------------------------------------------------------

    def get_bot_name(self) -> str:
        return "OmniMHXY"

    def get_bot_commands(self) -> list[BotCommand]:
        return [
            BotCommand("start", "🏠 唤醒主控台"),
            BotCommand("status", "🤖 查询当前模型"),
            BotCommand("model", "🤖 切换 LLM 模型"),
            BotCommand("vlmodel", "👁️ 切换视觉模型"),
            BotCommand("new", "🗑️ 清空对话历史"),
            BotCommand("tasks", "📋 列出可用自动化任务"),
            BotCommand("run_mijing", "🎮 执行秘境降妖（可选 port 参数）"),
            BotCommand("task_status", "📊 查看最近任务结果"),
            BotCommand("stop_task", "🛑 停止当前任务"),
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
                InlineKeyboardButton("🎮 执行秘境降妖", callback_data="cmd_run_mijing"),
                InlineKeyboardButton("📊 最近任务状态", callback_data="cmd_task_status"),
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
        vl_cfg = vl_registry.current()
        running = "⏳ 任务执行中" if self._task_running else "💤 空闲"
        return (
            f"🤖 当前模型：<b>{cfg.display_name}</b>\n"
            f"👁️ 视觉模型：<b>{vl_cfg.display_name}</b>\n"
            f"🎮 任务状态：{running}"
        )

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

    async def handle_button_click(self, update, context) -> None:
        query = update.callback_query
        if query and query.data and (
            query.data.startswith("switch_model:")
            or query.data.startswith("switch_vl_model:")
        ):
            return  # 由 register_model_switch 的 group=-1 handler 处理
        await super().handle_button_click(update, context)

    async def setup_extra_handlers(self, app: Application) -> None:
        register_model_switch(app, registry)
        register_model_switch(
            app,
            vl_registry,
            command="vlmodel",
            callback_prefix="switch_vl_model",
            title="👁️ 视觉模型管理",
        )
        app.add_handler(CommandHandler("tasks", self.tasks_command))
        app.add_handler(CommandHandler("run_mijing", self.run_mijing_command))
        app.add_handler(CommandHandler("task_status", self.task_status_command))
        app.add_handler(CommandHandler("stop_task", self.stop_task_command))

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
        if cmd == "cmd_run_mijing":
            if query.message:
                await query.message.reply_text(
                    "🚀 开始执行秘境降妖（全部实例）...\n"
                    "请使用 /run_mijing 命令触发（按钮触发暂不支持后台等待）。"
                )
            return ""
        if cmd == "cmd_task_status":
            if query.message:
                if self._task_running:
                    await query.message.reply_text("⏳ 任务正在执行中...")
                elif not self._last_task_results:
                    await query.message.reply_text("暂无任务记录。")
                else:
                    summary = self._format_task_results(self._last_task_results)
                    await query.message.reply_text(
                        f"<b>📊 最近一次任务结果</b>\n\n{summary}",
                        parse_mode=ParseMode.HTML,
                    )
            return ""
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
        memory_dir=MEMORY_DIR,
    )
    bot.run()


if __name__ == "__main__":
    main()

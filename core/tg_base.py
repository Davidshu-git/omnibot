"""
Telegram Bot 基础类 - 提供所有 bot 共用的渲染引擎、认证、Agent 执行、消息路由等基础设施。

各 bot 继承 TelegramBotBase 并重写钩子方法即可获得完整能力：
  - get_bot_name()            Bot 名称（用于 UI 占位符）
  - get_bot_commands()        左下角菜单命令列表
  - get_dashboard_keyboard()  主控台 Inline Keyboard 按钮
  - get_welcome_text()        欢迎语模板
  - get_tool_status_map()     工具调用状态文字映射（追加到默认映射）
  - setup_extra_handlers()    注册额外命令 / 消息处理器
  - setup_job_queue()         注册额外定时任务
"""
import ast
import os
import re
import sys
import time
import json
import uuid
import shutil
import logging
import asyncio
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core.observability import OmniObserver, OmnibotObsCallbackHandler, extract_think_blocks, strip_think_blocks

import markdown
from filelock import FileLock
from playwright.async_api import async_playwright

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, BotCommand, Bot,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler,
)
from langchain_core.callbacks import AsyncCallbackHandler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模块级纯函数（无状态，可被 broadcast_to_telegram 等独立调用）
# ---------------------------------------------------------------------------

async def keep_typing_action(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """每隔 4 秒发送一次 typing 心跳，直到被取消。"""
    try:
        while True:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


def translate_to_telegram_html(text: str) -> str:
    """将标准 Markdown 安全地转换为 Telegram HTML 方言。"""
    if not text:
        return text

    # 过滤推理模型的思考链（MiniMax-M2.7 / DeepSeek-R1 等输出的 <think>...</think>）
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    text = re.sub(
        r'```(\w+)?\n(.*?)```',
        lambda m: f'<pre><code class="language-{m.group(1) or "text"}">{m.group(2)}</code></pre>',
        text, flags=re.DOTALL
    )
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)

    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'~~(.*?)~~', r'<s>\1</s>', text)

    text = re.sub(r'^#{4,}\s+(.*)', r'<b>◈ \1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^###\s+(.*)', r'<blockquote><b>■ \1</b></blockquote>', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s+(.*)', r'<blockquote><b>● \1</b></blockquote>', text, flags=re.MULTILINE)
    text = re.sub(r'^#\s+(.*)', r'<blockquote><b>◆ \1</b></blockquote>', text, flags=re.MULTILINE)

    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)

    text = re.sub(r'^([ \t]{1,})[-*]\s+', r'\1└─ ▫️ ', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*]\s+', r'🔹 ', text, flags=re.MULTILINE)

    text = re.sub(r'^[-*_]{3,}\s*\n?', '', text, flags=re.MULTILINE)

    return text


async def send_with_caption_split(
    message: Message,
    photo,
    caption: str,
    max_length: int = 1024,
) -> None:
    """发送图片，caption 超长时自动拆分为多条消息。"""
    if len(caption) <= max_length:
        try:
            await message.reply_photo(
                photo=photo, caption=caption,
                parse_mode=ParseMode.HTML, show_caption_above_media=True,
            )
        except Exception as e:
            logger.warning(f"Caption HTML 渲染失败，降级为纯文本：{e}")
            fallback = re.sub(r'<[^>]+>', '', caption)
            await message.reply_photo(photo=photo, caption=fallback, show_caption_above_media=True)
    else:
        caption_part1 = caption[:max_length - 3] + "..."
        try:
            await message.reply_photo(
                photo=photo, caption=caption_part1,
                parse_mode=ParseMode.HTML, show_caption_above_media=True,
            )
        except Exception as e:
            logger.warning(f"Caption HTML 渲染失败，降级为纯文本：{e}")
            fallback = re.sub(r'<[^>]+>', '', caption_part1)
            await message.reply_photo(photo=photo, caption=fallback, show_caption_above_media=True)

        await asyncio.sleep(0.2)

        remaining = caption[max_length - 3:]
        while remaining:
            chunk = remaining[:4096]
            remaining = remaining[4096:]
            try:
                await message.reply_text(chunk, parse_mode=ParseMode.HTML)
            except Exception:
                await message.reply_text(re.sub(r'<[^>]+>', '', chunk))
            await asyncio.sleep(0.2)


async def render_markdown_table_to_image(
    text: str,
    sandbox_dir: Path,
) -> tuple[str, list[str]]:
    """
    将文本中的 Markdown 表格渲染为 PNG 图片（Playwright 无头浏览器）。

    Args:
        text:        包含 Markdown 表格的原始文本
        sandbox_dir: 图片输出目录

    Returns:
        (processed_text, image_paths)
        - processed_text: 表格已被 ![](./xxx.png) 语法替换后的文本
        - image_paths:    生成的临时 PNG 绝对路径列表（调用方负责删除）
    """
    table_pattern = re.compile(
        r'((?:\|.*\|\n)+[ \t]*\|?[ \t]*[-:]+[-| :]*\|?\n(?:\|.*\|\n?)+)'
    )
    matches = table_pattern.findall(text)

    if not matches:
        return text, []

    image_paths: list[str] = []

    css = """
    :root { --bg: #1A1D21; --border: #2D3239; --text: #E3E5E8; --header-bg: #22262B; --stripe: #1E2126; }
    html, body {
        background-color: var(--bg);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        margin: 0; padding: 20px;
        -webkit-font-smoothing: antialiased;
    }
    #capture_area {
        width: max-content;
        background-color: var(--bg);
        padding: 16px;
        border: 1px solid var(--border);
        box-sizing: border-box;
        overflow: hidden;
    }
    table { border-collapse: collapse; color: var(--text); font-size: 14px; margin: 0; }
    th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid var(--border); }
    th {
        background-color: var(--header-bg); font-weight: 600; color: #A0A5AD;
        text-transform: uppercase; font-size: 12px; letter-spacing: 0.5px;
    }
    tr:last-child td { border-bottom: none; }
    tr:nth-child(even) td { background-color: var(--stripe); }
    """

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
        page = await browser.new_page(device_scale_factor=3)

        for idx, md_table in enumerate(matches):
            try:
                html_table = markdown.markdown(md_table, extensions=['tables'])
                full_html = (
                    f"<!DOCTYPE html><html><head><style>{css}</style></head>"
                    f"<body><div id='capture_area'>{html_table}</div></body></html>"
                )
                await page.set_content(full_html)

                img_filename = f"table_render_{int(time.time())}_{idx}.png"
                img_path = (sandbox_dir / img_filename).resolve()

                element = await page.wait_for_selector('#capture_area')
                await element.evaluate(
                    "el => el.style.width = Math.ceil(el.getBoundingClientRect().width) + 'px'"
                )
                await element.screenshot(path=str(img_path), omit_background=True)

                image_paths.append(str(img_path))
                text = text.replace(md_table, f"\n\n![表格](./{img_filename})\n\n")

            except Exception as e:
                logger.error(f"Playwright 表格渲染失败：{e}")
                continue

        await browser.close()

    return text, image_paths


async def convert_md_to_pdf(md_path: Path, sandbox_dir: Path) -> Path:
    """
    将 .md 文件渲染为 PDF（Playwright 无头浏览器），内嵌本地图片。

    Args:
        md_path:     源 .md 文件绝对路径
        sandbox_dir: 图片资源目录（解析相对路径用）

    Returns:
        生成的 .pdf 文件路径（与 .md 同目录）
    """
    md_text = md_path.read_text(encoding='utf-8')
    html_body = markdown.markdown(md_text, extensions=['tables', 'fenced_code'])

    def resolve_img_src(match: re.Match) -> str:
        src = match.group(1)
        img_file = (sandbox_dir / Path(src).name).resolve()
        if img_file.exists():
            return f'src="file://{img_file}"'
        return match.group(0)

    html_body = re.sub(r'src="([^"]+)"', resolve_img_src, html_body)

    css = """
    body {
        font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
                     "Microsoft YaHei", "Segoe UI", Roboto, Arial, sans-serif;
        font-size: 14px; line-height: 1.8; color: #222;
        max-width: 860px; margin: 0 auto; padding: 0 8px;
    }
    h1 { font-size: 22px; border-bottom: 2px solid #333; padding-bottom: 6px; }
    h2 { font-size: 18px; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin-top: 24px; }
    h3 { font-size: 15px; margin-top: 18px; }
    table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }
    th, td { border: 1px solid #ccc; padding: 8px 12px; text-align: left; }
    th { background: #f0f0f0; font-weight: 600; }
    tr:nth-child(even) td { background: #fafafa; }
    img { max-width: 100%; height: auto; display: block; margin: 12px auto; }
    code { background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-size: 12px; }
    pre { background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }
    pre code { background: none; padding: 0; }
    hr { border: none; border-top: 1px solid #ddd; margin: 20px 0; }
    blockquote { border-left: 3px solid #aaa; margin: 0; padding-left: 12px; color: #555; }
    """

    full_html = (
        f"<!DOCTYPE html>\n<html><head>\n<meta charset=\"utf-8\">\n"
        f"<style>{css}</style>\n</head><body>{html_body}</body></html>"
    )

    pdf_path = md_path.with_suffix('.pdf')
    tmp_html = md_path.with_suffix('.tmp.html')

    try:
        tmp_html.write_text(full_html, encoding='utf-8')
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
            page = await browser.new_page()
            await page.goto(f'file://{tmp_html}', wait_until='networkidle')
            await page.pdf(
                path=str(pdf_path),
                format='A4',
                margin={'top': '18mm', 'bottom': '18mm', 'left': '15mm', 'right': '15mm'},
                print_background=True,
            )
            await browser.close()
    finally:
        tmp_html.unlink(missing_ok=True)

    return pdf_path


async def broadcast_to_telegram(
    text: str,
    bot_token: str,
    allowed_user_ids: list[int],
    sandbox_dir: Path,
) -> dict[str, list[int]]:
    """
    跨进程推送引擎：供 daily_job 在独立进程中直接调用。

    Args:
        text:             待发送的 Markdown 文本
        bot_token:        Telegram Bot Token
        allowed_user_ids: 推送目标用户 ID 列表
        sandbox_dir:      图片输出目录（用于表格渲染）

    Returns:
        dict，包含 "success" 和 "failed" 两个 user_id 列表。
    """
    if not bot_token or not allowed_user_ids:
        logger.warning("未配置 bot_token 或 allowed_user_ids，跳过推送。")
        return {"success": [], "failed": []}

    bot = Bot(token=bot_token)

    final_text, table_render_paths = await render_markdown_table_to_image(text, sandbox_dir)
    chunks = re.split(r'(!\[.*?\]\(.*?\))', final_text)

    success_ids: list[int] = []
    failed_ids: list[int] = []

    for user_id in allowed_user_ids:
        try:
            await bot.send_message(
                chat_id=user_id,
                text="<blockquote><b>🔔 每日报告已送达</b></blockquote>",
                parse_mode=ParseMode.HTML,
            )

            is_consumed = [False] * len(chunks)
            for i, chunk in enumerate(chunks):
                chunk = chunk.strip()
                if not chunk:
                    continue

                img_match = re.match(r'^!\[.*?\]\((.*?)\)$', chunk)
                if img_match:
                    img_filename = img_match.group(1).replace("./", "")
                    img_path = (sandbox_dir / img_filename).resolve()

                    raw_caption = ""
                    if i > 0 and not is_consumed[i - 1]:
                        prev_chunk = chunks[i - 1].strip()
                        if not re.match(r'^!\[.*?\]\(.*?\)$', prev_chunk):
                            raw_caption = prev_chunk

                    if img_path.exists() and img_path.stat().st_size > 0:
                        with open(img_path, 'rb') as photo:
                            if raw_caption:
                                html_caption = translate_to_telegram_html(raw_caption)
                                try:
                                    if len(html_caption) <= 1024:
                                        await bot.send_photo(
                                            chat_id=user_id, photo=photo,
                                            caption=html_caption, parse_mode=ParseMode.HTML,
                                            show_caption_above_media=True,
                                        )
                                    else:
                                        await bot.send_photo(
                                            chat_id=user_id, photo=photo,
                                            caption=html_caption[:1021] + "...",
                                            parse_mode=ParseMode.HTML,
                                            show_caption_above_media=True,
                                        )
                                        await bot.send_message(
                                            chat_id=user_id,
                                            text=html_caption[1021:],
                                            parse_mode=ParseMode.HTML,
                                        )
                                except Exception:
                                    fallback = re.sub(r'<[^>]+>', '', html_caption)
                                    await bot.send_photo(
                                        chat_id=user_id, photo=photo,
                                        caption=fallback[:1024], show_caption_above_media=True,
                                    )
                                is_consumed[i - 1] = True
                            else:
                                await bot.send_photo(chat_id=user_id, photo=photo)
                    else:
                        logger.warning(f"图片文件不存在或为空：{img_path}")
                    await asyncio.sleep(0.3)

                else:
                    next_is_image = (
                        i + 1 < len(chunks)
                        and re.match(r'^!\[.*?\]\(.*?\)$', chunks[i + 1].strip())
                    )
                    if next_is_image:
                        continue

                    html_text = translate_to_telegram_html(chunk)
                    try:
                        await bot.send_message(
                            chat_id=user_id, text=html_text, parse_mode=ParseMode.HTML
                        )
                    except Exception:
                        fallback = re.sub(r'<[^>]+>', '', html_text)
                        await bot.send_message(chat_id=user_id, text=fallback)
                    await asyncio.sleep(0.3)

            success_ids.append(user_id)

        except Exception as e:
            logger.error(f"向用户 {user_id} 推送失败：{e}")
            failed_ids.append(user_id)

    for _tmp_path in table_render_paths:
        try:
            Path(_tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

    return {"success": success_ids, "failed": failed_ids}


# ---------------------------------------------------------------------------
# AsyncTelegramCallbackHandler
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_STATUS_MAP: dict[str, str] = {
    "read_local_file": "📂 正在穿透沙箱读取本地文件...",
    "write_local_file": "📝 正在排版并生成最终深度报告...",
    "list_kb_files": "🗂️ 正在扫描知识库文件索引...",
    "analyze_local_document": "📚 正在穿透本地向量库检索文档...",
    "rename_kb_file": "✏️ 正在重命名知识库文件并清理旧缓存...",
    "update_user_memory": "🧠 正在将关键信息写入长期记忆库...",
    "append_transaction_log": "📜 正在追加交易日志流水账...",
    "trigger_job": "🚀 正在将任务投递至独立进程...",
    "query_job_status": "📡 正在追踪后台任务执行状态...",
}


class AsyncTelegramCallbackHandler(AsyncCallbackHandler):
    """拦截 Agent 异步执行流，将工具调用状态实时回传到 Telegram 屏幕。"""

    def __init__(
        self,
        status_message: Message,
        tool_status_map: dict[str, str] | None = None,
        bot_name: str = "OmniBot",
    ) -> None:
        self.status_message = status_message
        self.last_update_time = 0.0
        self.written_md_files: set[str] = set()
        self.bot_name = bot_name
        self._tool_map = {**_DEFAULT_TOOL_STATUS_MAP, **(tool_status_map or {})}

    async def _safe_edit(self, text: str) -> None:
        """防抖更新，最高 1 次/秒，避免触发 Telegram API 频率限制。"""
        current_time = time.time()
        if current_time - self.last_update_time < 1.0:
            await asyncio.sleep(1.0 - (current_time - self.last_update_time))
        try:
            await self.status_message.edit_text(text, parse_mode=ParseMode.HTML)
            self.last_update_time = time.time()
        except Exception:
            pass

    async def on_tool_start(self, serialized: dict, input_str: str, **kwargs) -> None:
        tool_name = serialized.get("name", "tool")

        if tool_name == "write_local_file":
            try:
                params = ast.literal_eval(input_str)
                file_path = params.get("file_path", "")
                if file_path.endswith(".md"):
                    self.written_md_files.add(Path(file_path).name)
            except (ValueError, SyntaxError, AttributeError):
                pass

        msg = self._tool_map.get(tool_name, f"⚡ 正在挂载系统组件：{tool_name}...")
        ui_text = (
            f"<blockquote><b>🤖 {self.bot_name} 引擎运转中...</b></blockquote>\n"
            f"<i>{msg}</i>"
        )
        await self._safe_edit(ui_text)

    async def on_tool_end(self, output: str, **kwargs) -> None:
        ui_text = (
            f"<blockquote><b>🤖 {self.bot_name} 引擎运转中...</b></blockquote>\n"
            f"<i>✔️ 数据提取完毕，正在进行逻辑推理...</i>"
        )
        await self._safe_edit(ui_text)


# ---------------------------------------------------------------------------
# TelegramBotBase
# ---------------------------------------------------------------------------

class TelegramBotBase:
    """
    Telegram Bot 基础类。

    子类通过重写钩子方法注入领域专属内容：
      - get_bot_name()
      - get_bot_commands()
      - get_dashboard_keyboard()
      - get_welcome_text()
      - get_tool_status_map()
      - setup_extra_handlers()
      - setup_job_queue()
    """

    def __init__(
        self,
        bot_token: str,
        allowed_user_ids: list[int],
        agent,
        get_user_profile_fn: Callable[[], str],
        sandbox_dir: Path,
        kb_dir: Path,
        job_module: str,
        job_func: str = "job_routine",
        asr_api_key: str = "",
        obs_dir: Optional[Path] = None,
        agent_id: str = "",
    ) -> None:
        self.bot_token = bot_token
        self.allowed_user_ids = allowed_user_ids
        self.agent = agent
        self.get_user_profile_fn = get_user_profile_fn
        self.sandbox_dir = sandbox_dir
        self.kb_dir = kb_dir
        self.job_module = job_module
        self.job_func = job_func
        self.asr_api_key = asr_api_key
        self.obs_dir = obs_dir
        self.agent_id = agent_id
        # 暂存待确认的重复上传（user_id → 上传元信息），内存级，重启失效
        self._pending_uploads: dict[int, dict] = {}

    # ------------------------------------------------------------------
    # 钩子方法（子类可重写）
    # ------------------------------------------------------------------

    def get_bot_name(self) -> str:
        """Bot UI 名称，出现在状态占位符中。"""
        return "OmniBot"

    def get_bot_commands(self) -> list[BotCommand]:
        """左下角菜单命令列表。"""
        return [
            BotCommand("start", "🏠 唤醒主控台"),
            BotCommand("status", "📊 查询最新任务进度"),
        ]

    def get_dashboard_keyboard(self) -> list[list[InlineKeyboardButton]]:
        """主控台 Inline Keyboard 按钮布局。"""
        return [
            [InlineKeyboardButton("📝 触发后台任务", callback_data="cmd_trigger_job")],
            [InlineKeyboardButton("📊 查询最新任务进度", callback_data="cmd_status")],
        ]

    def get_welcome_text(self, first_name: str) -> str:
        """欢迎语 HTML 文本。"""
        return (
            f"<blockquote><b>🚀 {self.get_bot_name()} 已挂载</b></blockquote>\n"
            f"你好 <b>{first_name}</b>，连接安全。\n\n"
            f"<i>您可以直接输入自然语言下达指令，或通过下方面板执行核心任务：</i>"
        )

    def get_tool_status_map(self) -> dict[str, str]:
        """追加到默认工具状态映射的领域专属条目（子类重写）。"""
        return {}

    async def setup_extra_handlers(self, app: Application) -> None:
        """注册额外命令 / 消息处理器（子类重写）。"""
        pass

    async def setup_job_queue(self, app: Application) -> None:
        """注册额外定时任务（子类重写）。"""
        pass

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _is_authorized(self, user_id: int) -> bool:
        return user_id in self.allowed_user_ids

    def _read_job_status_sync(self, job_id: str) -> str:
        """零延迟读取本地 JSON 任务状态文件，绝不调用大模型。"""
        try:
            status_file = Path(f"./jobs/status/{job_id}.json").resolve()
            if not status_file.exists():
                return f"❌ 未找到任务 <code>{job_id}</code> 的状态文件。"

            with open(status_file, 'r', encoding='utf-8') as f:
                status = json.load(f)

            status_map = {
                "pending": "⏳ 等待分配资源...",
                "running": "🔄 任务正在执行中...",
                "completed": "✅ 任务已完成！",
                "failed": "❌ 任务执行失败",
            }

            current_status = status_map.get(status.get('status'), '未知状态')
            result = (
                f"<blockquote><b>📊 任务状态：{job_id}</b></blockquote>\n"
                f"<b>当前状态：</b>{current_status}\n"
                f"<b>创建时间：</b><code>{status.get('created_at', 'N/A')}</code>\n"
            )
            if status.get('started_at'):
                result += f"<b>启动时间：</b><code>{status.get('started_at')}</code>\n"
            if status.get('completed_at'):
                result += f"<b>完成时间：</b><code>{status.get('completed_at')}</code>\n"
            if status.get('error'):
                result += f"<b>异常抛出：</b><code>{status.get('error')}</code>\n"

            return result
        except Exception as e:
            return f"❌ 状态读取异常：{e}"

    def _get_latest_job_id(self) -> str:
        """扫描本地状态目录，获取最新提交的任务 ID。"""
        status_dir = Path("./jobs/status").resolve()
        if not status_dir.exists():
            return ""
        files = list(status_dir.glob("*.json"))
        if not files:
            return ""
        latest_file = max(files, key=os.path.getmtime)
        return latest_file.stem

    async def _execute_kb_upload(
        self,
        message: Message,
        user_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        update: Update,
        file_id: str,
        file_name: str,
        save_path: Path,
        file_size_mb: float,
    ) -> None:
        """下载文件至知识库并触发 RAG 摘要（handle_document 与按钮回调的公共执行体）。"""
        status_msg = await message.reply_text(
            f"⏳ <b>正在拉取文件：</b>{file_name} ({file_size_mb:.1f}MB)",
            parse_mode=ParseMode.HTML,
        )
        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=save_path)

            await status_msg.edit_text(
                f"<blockquote><b>⚡ 文件已挂载至知识库：{file_name}</b></blockquote>\n"
                f"<i>🧠 正在唤醒 Embedding 引擎进行 RAG 深度阅读，请稍候...</i>",
                parse_mode=ParseMode.HTML,
            )

            rag_prompt = (
                f"我已经把一份名为 '{file_name}' 的文件放进了知识库。"
                f"请调用 analyze_local_document 工具，仔细阅读这篇文档，"
                f"并给我一份结构化的核心内容摘要。"
                f"另外，如果文件名看起来是乱码或无意义的随机字符串（如哈希值、纯数字串等），"
                f"请根据文档内容自动推断一个简洁的中文名称（如'阿里巴巴2026中期财报.pdf'），"
                f"并调用 rename_kb_file 工具完成重命名，在摘要中说明新文件名。"
            )
            await self.execute_agent_task(rag_prompt, message, user_id, context, update)

            try:
                await status_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"文件接收与解析失败：{e}")
            await status_msg.edit_text(f"❌ 文件处理失败：{type(e).__name__}")

    async def _handle_status_query(self, message: Message) -> None:
        """内部函数：处理状态查询并回复（可被命令和按钮复用）。"""
        latest_job_id = self._get_latest_job_id()
        if not latest_job_id:
            await message.reply_text("📭 当前系统没有任何后台任务记录。")
            return

        status_text = self._read_job_status_sync(latest_job_id)
        refresh_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 实时刷新任务进度", callback_data="check_job:latest")]
        ])
        await message.reply_text(status_text, parse_mode=ParseMode.HTML, reply_markup=refresh_keyboard)

    async def _dispatch_job_task(self, message: Message, job_id: str) -> None:
        """派发后台任务至独立进程（脊髓反射，绕过大模型）。"""
        status_dir = Path("./jobs/status").resolve()
        status_dir.mkdir(parents=True, exist_ok=True)
        initial_status = {
            "job_id": job_id,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }
        with open(status_dir / f"{job_id}.json", 'w', encoding='utf-8') as f:
            json.dump(initial_status, f, ensure_ascii=False, indent=2)

        log_dir = Path("./jobs/logs").resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        stderr_log = open(log_dir / f"{job_id}_stderr.log", 'w', encoding='utf-8')

        subprocess.Popen(
            [
                sys.executable, "-m", "core.job_runner",
                "--job-id", job_id,
                "--job-module", self.job_module,
                "--job-func", self.job_func,
            ],
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            start_new_session=True,
        )
        stderr_log.close()

        reply_text = (
            f"✅ 任务已成功挂载至后台独立进程！\n\n"
            f"**任务 ID**: <code>{job_id}</code>\n\n"
            f"大约 3~4 分钟后任务完成，结果将自动推送。"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 实时刷新任务进度", callback_data=f"check_job:{job_id}")]
        ])
        await message.reply_text(reply_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def _send_dashboard(self, message: Message, first_name: str) -> None:
        """下发全息操控面板。"""
        reply_markup = InlineKeyboardMarkup(self.get_dashboard_keyboard())
        await message.reply_text(
            self.get_welcome_text(first_name),
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

    # ------------------------------------------------------------------
    # 公共 Agent 执行引擎
    # ------------------------------------------------------------------

    async def execute_agent_task(
        self,
        user_msg: str,
        message: Message,
        user_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        update: Update,
    ) -> None:
        """
        核心 Agent 任务执行流，文本消息和按钮点击均可复用。

        Args:
            user_msg: 用户输入或按钮映射的 Prompt
            message:  用于发送回复的 Message 对象
            user_id:  用于 session 隔离的用户 ID
            context:  Telegram Context
            update:   Telegram Update
        """
        logger.info(f"[execute_agent_task] user_id={user_id}, msg_length={len(user_msg)}")

        effective_chat = update.effective_chat
        if effective_chat is None:
            logger.warning("无法获取 effective_chat，终止任务")
            return

        chat_id = effective_chat.id
        typing_task = asyncio.create_task(keep_typing_action(chat_id, context))
        status_msg: Optional[Message] = None

        # memory_session_id: used by LangChain STM — must stay stable across bot upgrades
        memory_session_id = f"tg_session_{user_id}"
        # obs_session_id: includes agent_slug to avoid cross-bot PK collision in DB
        agent_slug = self.agent_id.replace("-", "_") if self.agent_id else "bot"
        obs_session_id = f"tg_session_{agent_slug}_{user_id}"
        trace_id = f"{obs_session_id}:t{int(time.time() * 1000)}"
        obs: Optional[OmniObserver] = None
        if self.obs_dir is not None and self.agent_id:
            obs = OmniObserver(obs_session_id, self.agent_id, self.obs_dir)
            obs.log_message("user", user_msg, trace_id=trace_id)

        try:
            status_msg = await message.reply_text(
                f"<blockquote><b>🤖 {self.get_bot_name()} 引擎已唤醒</b></blockquote>\n"
                f"<i>⏳ 正在建立神经连接...</i>",
                parse_mode=ParseMode.HTML,
            )

            tg_callback = AsyncTelegramCallbackHandler(
                status_msg,
                tool_status_map=self.get_tool_status_map(),
                bot_name=self.get_bot_name(),
            )
            callbacks = [tg_callback]
            if obs is not None:
                callbacks.append(OmnibotObsCallbackHandler(obs, trace_id))

            response = await self.agent.ainvoke(
                {
                    "input": user_msg,
                    "user_profile": self.get_user_profile_fn(),
                    "current_time": datetime.now().strftime("%Y年%m月%d日 %H:%M:%S"),
                },
                config={
                    "configurable": {"session_id": memory_session_id},
                    "callbacks": callbacks,
                },
            )

            reply_text = response['output']

            # Extract <think>…</think> BEFORE translate_to_telegram_html strips it
            if obs is not None:
                for think_content in extract_think_blocks(reply_text):
                    obs.log_thought(think_content, trace_id=trace_id)

            if obs is not None:
                obs.log_message("assistant", strip_think_blocks(reply_text), trace_id=trace_id)

            await status_msg.delete()

            final_text, table_render_paths = await render_markdown_table_to_image(
                reply_text, self.sandbox_dir
            )

            chunks = re.split(r'(!\[.*?\]\(.*?\))', final_text)
            is_consumed = [False] * len(chunks)

            for i, chunk in enumerate(chunks):
                chunk = chunk.strip()
                if not chunk:
                    continue

                img_match = re.match(r'^!\[.*?\]\((.*?)\)$', chunk)

                if img_match:
                    img_filename = img_match.group(1).replace("./", "")
                    img_path = (self.sandbox_dir / img_filename).resolve()

                    raw_caption = ""
                    if i > 0 and not is_consumed[i - 1]:
                        prev_chunk = chunks[i - 1].strip()
                        if not re.match(r'^!\[.*?\]\(.*?\)$', prev_chunk):
                            raw_caption = prev_chunk

                    if img_path.exists():
                        try:
                            with open(img_path, 'rb') as photo:
                                if raw_caption:
                                    html_caption = translate_to_telegram_html(raw_caption)
                                    await send_with_caption_split(message, photo, html_caption)
                                    is_consumed[i - 1] = True
                                else:
                                    await message.reply_photo(photo=photo)
                        except Exception as e:
                            logger.error(f"发送图片失败：{e}")
                    else:
                        await message.reply_text("⚠️ [此处图片生成失败或已被清理]")

                    await asyncio.sleep(0.2)

                else:
                    next_is_image = (
                        i + 1 < len(chunks)
                        and re.match(r'^!\[.*?\]\(.*?\)$', chunks[i + 1].strip())
                    )
                    if next_is_image:
                        continue

                    html_text = translate_to_telegram_html(chunk)
                    try:
                        await message.reply_text(html_text, parse_mode=ParseMode.HTML)
                    except Exception as e:
                        logger.warning(f"HTML 渲染失败，降级为纯文本：{e}")
                        await message.reply_text(re.sub(r'<[^>]+>', '', html_text))

                    await asyncio.sleep(0.2)

            for _tmp_path in table_render_paths:
                try:
                    Path(_tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

            if tg_callback.written_md_files:
                buttons = [
                    [InlineKeyboardButton(f"📄 {md_name}", callback_data=f"send_file:{md_name}")]
                    for md_name in tg_callback.written_md_files
                ]
                await message.reply_text(
                    "📁 以下报告文件已生成，点击即可获取：",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

        except Exception as e:
            logger.error(f"[execute_agent_task] 处理失败：{e}")
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            await message.reply_text(f"⚠️ 系统熔断：{str(e)}")
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Telegram 事件处理器
    # ------------------------------------------------------------------

    async def _post_init(self, application: Application) -> None:
        """Bot 启动钩子：注入左下角全局菜单、额外处理器与定时任务。"""
        await application.bot.set_my_commands(self.get_bot_commands())
        logger.info("✅ Bot Commands 注入成功")
        await self.setup_extra_handlers(application)
        await self.setup_job_queue(application)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return
        await self._send_dashboard(message, user.first_name)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return
        await self._handle_status_query(message)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return
        user_msg = message.text or ""
        logger.info(f"收到用户 {user.id} 的消息：{user_msg}")
        await self.execute_agent_task(user_msg, message, user.id, context, update)

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """拦截用户上传的文件，存入知识库并触发 RAG 摘要。"""
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return

        doc = message.document
        if doc is None:
            return

        raw_name = doc.file_name or f"upload_{int(time.time())}.txt"
        # 策略 A：NFC 统一编码 + 去首尾空格 + 内部空格→下划线，防止 LLM 文件名幻觉
        file_name = unicodedata.normalize('NFC', raw_name).strip().replace(' ', '_')
        file_size_mb = doc.file_size / (1024 * 1024) if doc.file_size else 0

        if file_size_mb > 20.0:
            await message.reply_text(
                f"⚠️ 系统熔断：文件高达 {file_size_mb:.1f}MB，超出 Telegram 20MB 限制。"
            )
            return

        allowed_exts = {'.pdf', '.md', '.txt', '.csv'}
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in allowed_exts:
            await message.reply_text(f"⚠️ 格式拒绝：暂不支持 {ext} 格式进行向量化。")
            return

        kb_dir_resolved = self.kb_dir.resolve()
        save_path = (kb_dir_resolved / file_name).resolve()

        if not save_path.is_relative_to(kb_dir_resolved):
            await message.reply_text("❌ 安全拦截：非法的文件名，已销毁。")
            return

        # ── 重复文件检测 ────────────────────────────────────────────────
        if save_path.exists():
            stat = save_path.stat()
            old_size_mb = stat.st_size / (1024 * 1024)
            age_days = (time.time() - stat.st_mtime) / 86400

            self._pending_uploads[user.id] = {
                "file_id": doc.file_id,
                "file_name": file_name,
                "save_path": str(save_path),
                "file_size_mb": file_size_mb,
            }

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 覆盖旧文件", callback_data=f"dup_overwrite:{user.id}"),
                InlineKeyboardButton("📋 重命名保留", callback_data=f"dup_rename:{user.id}"),
            ]])
            await message.reply_text(
                f"⚠️ <b>知识库中已存在同名文件</b>\n\n"
                f"<b>文件名：</b><code>{file_name}</code>\n"
                f"<b>原文件：</b>{old_size_mb:.1f} MB，{age_days:.0f} 天前上传\n"
                f"<b>新文件：</b>{file_size_mb:.1f} MB\n\n"
                f"请选择操作：",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        # ────────────────────────────────────────────────────────────────

        await self._execute_kb_upload(
            message, user.id, context, update,
            doc.file_id, file_name, save_path, file_size_mb,
        )

    async def handle_button_click(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """拦截 Inline 按钮点击，路由至对应处理逻辑。"""
        query = update.callback_query
        if query is None:
            return

        await query.answer()

        user_id = query.from_user.id if query.from_user else None
        if user_id is None or not self._is_authorized(user_id):
            logger.warning(f"⛔ 未授权按钮点击 | User ID: {user_id}")
            return

        cmd = query.data
        if cmd is None:
            return

        # 刷新按钮保留 markup，其余按钮立刻清除
        if not cmd.startswith("check_job:"):
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as e:
                logger.warning(f"清除按钮失败：{e}")

        # 任务状态刷新（旁路直通，不走大模型）
        if cmd.startswith("check_job:"):
            parts = cmd.split(":", 1)
            if len(parts) < 2:
                return
            job_id = parts[1]
            if job_id == "latest":
                job_id = self._get_latest_job_id()
                if not job_id:
                    await query.message.edit_text("📭 当前系统没有任何后台任务记录。", parse_mode=ParseMode.HTML)
                    return

            status_text = self._read_job_status_sync(job_id)
            refresh_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 实时刷新任务进度", callback_data=cmd)]
            ])
            try:
                await query.message.edit_text(
                    status_text, parse_mode=ParseMode.HTML, reply_markup=refresh_keyboard
                )
            except Exception:
                pass
            return

        # .md 文件按需下载（转 PDF 后发送）
        if cmd.startswith("send_file:"):
            md_name = cmd.split(":", 1)[1]
            md_path = (self.sandbox_dir / md_name).resolve()
            if not md_path.exists():
                await query.message.reply_text(f"⚠️ 文件不存在或已被清理：{md_name}")
                return
            try:
                await query.message.reply_text("⏳ 正在渲染 PDF，请稍候...")
                pdf_path = await convert_md_to_pdf(md_path, self.sandbox_dir)
                with open(pdf_path, 'rb') as doc:
                    await query.message.reply_document(
                        document=doc,
                        filename=pdf_path.name,
                        caption=f"📑 <b>{pdf_path.name}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                archive_keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📚 归档到知识库", callback_data=f"archive_file:{md_name}"),
                    InlineKeyboardButton("跳过", callback_data="archive_skip"),
                ]])
                await query.message.reply_text(
                    "是否将此报告归档到知识库供日后检索？",
                    reply_markup=archive_keyboard,
                )
            except Exception as e:
                logger.error(f"PDF 转换或发送失败 [{md_name}]：{e}")
                await query.message.reply_text(f"⚠️ PDF 生成失败：{e}")
            return

        # 归档到知识库
        if cmd.startswith("archive_file:"):
            md_name = cmd.split(":", 1)[1]
            md_path = (self.sandbox_dir / md_name).resolve()
            if not md_path.exists():
                await query.message.reply_text(f"⚠️ 源文件不存在：{md_name}")
                return
            try:
                dest = (self.kb_dir / md_name).resolve()
                shutil.copy2(str(md_path), str(dest))
                await query.message.reply_text(
                    f"✅ 已归档至知识库：<code>{md_name}</code>", parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"归档失败 [{md_name}]：{e}")
                await query.message.reply_text(f"⚠️ 归档失败：{e}")
            return

        if cmd == "archive_skip":
            await query.message.reply_text("已跳过归档。")
            return

        # 重复上传：覆盖 / 重命名
        if cmd.startswith("dup_overwrite:") or cmd.startswith("dup_rename:"):
            try:
                target_uid = int(cmd.split(":", 1)[1])
            except ValueError:
                return

            pending = self._pending_uploads.pop(target_uid, None)
            if pending is None:
                await query.message.reply_text("⚠️ 操作已失效，请重新上传文件。")
                return

            file_id: str = pending["file_id"]
            orig_file_name: str = pending["file_name"]
            file_size_mb: float = pending["file_size_mb"]

            if cmd.startswith("dup_overwrite:"):
                save_path = Path(pending["save_path"])
                upload_name = orig_file_name
                await query.message.reply_text(
                    f"🔄 将覆盖旧文件：<code>{orig_file_name}</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                original = Path(pending["save_path"])
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                upload_name = f"{original.stem}_{timestamp}{original.suffix}"
                save_path = (self.kb_dir.resolve() / upload_name).resolve()
                await query.message.reply_text(
                    f"📋 已重命名为：<code>{upload_name}</code>，两份文件均保留。",
                    parse_mode=ParseMode.HTML,
                )

            await self._execute_kb_upload(
                query.message, user_id, context, update,
                file_id, upload_name, save_path, file_size_mb,
            )
            return

        # 返回主控台
        if cmd == "cmd_home":
            try:
                await query.message.delete()
            except Exception:
                pass
            await self._send_dashboard(query.message, query.from_user.first_name)
            return

        # cmd_status：状态查询（旁路，不走大模型）
        if cmd == "cmd_status":
            if query.message and isinstance(query.message, Message):
                await self._handle_status_query(query.message)
            return

        # cmd_trigger_job：直接派发任务
        if cmd == "cmd_trigger_job":
            job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            if query.message and isinstance(query.message, Message):
                await self._dispatch_job_task(query.message, job_id)
            return

        # 委派给子类处理领域专属指令
        user_msg = await self.handle_custom_cmd(cmd, query, user_id, context, update)

        if user_msg and query.message:
            await query.message.reply_text(
                f"<blockquote><b>⚡ 面板指令注入：</b>\n<i>{user_msg}</i></blockquote>",
                parse_mode=ParseMode.HTML,
            )
            await self.execute_agent_task(user_msg, query.message, user_id, context, update)

    async def handle_custom_cmd(
        self,
        cmd: str,
        query,
        user_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        update: Update,
    ) -> str:
        """
        处理子类自定义按钮指令（子类重写）。

        Returns:
            用于传入 execute_agent_task 的 Prompt 字符串；
            若直接处理完毕（如旁路响应）则返回空字符串。
        """
        logger.warning(f"未知按钮指令：{cmd}")
        return ""

    # ------------------------------------------------------------------
    # 语音识别（DashScope Paraformer-v2）
    # ------------------------------------------------------------------

    def _transcribe_audio(self, file_path: str) -> str:
        """
        调用 Groq Whisper large-v3 转写本地音频文件（同步阻塞）。
        原生支持 Telegram OGG OPUS 格式，无需格式转换。

        Args:
            file_path: 本地音频文件路径（OGA/OGG 格式）

        Returns:
            识别后的文本字符串，失败时返回空字符串
        """
        from groq import Groq

        client = Groq(api_key=self.asr_api_key)
        with open(file_path, 'rb') as f:
            transcription = client.audio.transcriptions.create(
                file=(Path(file_path).name, f),
                model='whisper-large-v3',
            )
        return transcription.text

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """处理语音消息：下载 OGG → Paraformer 转写 → 交给 Agent。"""
        import tempfile

        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return
        if not self._is_authorized(user.id):
            await message.reply_text("⛔ 未授权访问")
            return
        if not self.asr_api_key:
            await message.reply_text("⚠️ 语音识别未配置，请联系管理员。")
            return

        voice = message.voice
        if voice is None:
            return

        status_msg = await message.reply_text("🎙️ 正在识别语音，请稍候...")
        tmp_path: Optional[str] = None
        try:
            voice_file = await context.bot.get_file(voice.file_id)
            with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp:
                tmp_path = tmp.name
            await voice_file.download_to_drive(tmp_path)

            text = await asyncio.to_thread(self._transcribe_audio, tmp_path)

            if not text.strip():
                await status_msg.edit_text("⚠️ 未识别到有效内容，请重试或改用文字输入。")
                return

            await status_msg.edit_text(f"🎙️ 已识别：<i>{text}</i>", parse_mode=ParseMode.HTML)
            logger.info(f"语音识别完成 user={user.id}: {text}")
            await self.execute_agent_task(text, message, user.id, context, update)

        except Exception as e:
            logger.error(f"语音处理失败：{e}")
            await status_msg.edit_text(f"❌ 语音识别失败：{type(e).__name__}")
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 错误处理器
    # ------------------------------------------------------------------

    async def error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error("未捕获的异常：", exc_info=context.error)
        if isinstance(context.error, Exception):
            error_type = type(context.error).__name__
            if "ConnectError" in error_type or "NetworkError" in error_type:
                logger.warning("检测到网络错误，临时 DNS 解析失败或 Telegram 不可达")
                return

    # ------------------------------------------------------------------
    # 启动入口
    # ------------------------------------------------------------------

    def run(self) -> None:
        """构建并启动 Bot（长轮询模式）。"""
        logger.info(f"启动 {self.get_bot_name()} Telegram Bot...")

        application = (
            Application.builder()
            .token(self.bot_token)
            .read_timeout(120)
            .write_timeout(120)
            .connect_timeout(60)
            .pool_timeout(120)
            .post_init(self._post_init)
            .build()
        )

        application.add_error_handler(self.error_handler)

        # 注册通用处理器
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("status", self.status_command))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )
        application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        application.add_handler(MessageHandler(filters.VOICE, self.handle_voice))
        application.add_handler(CallbackQueryHandler(self.handle_button_click))

        application.run_polling(allowed_updates=Update.ALL_TYPES)

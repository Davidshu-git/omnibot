"""
EHS Bot 定时任务 - 定期 EHS 简报生成与推送。

job_routine() 是标准入口，由 core.job_runner 通过 --job-module ehs_bot.daily_job 调用。
"""
import os
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import SecretStr

from core.notifier import send_market_report_email

load_dotenv()

logger = logging.getLogger(__name__)

DASHSCOPE_KEY = os.getenv("DASHSCOPE_API_KEY", "")
KB_DIR = Path("./data/ehs/knowledge_base").resolve()
SANDBOX_DIR = Path("./data/ehs/agent_workspace").resolve()


def job_routine() -> None:
    """
    EHS 定期简报生成与推送的主流程。

    由 core.job_runner 在独立进程中调用。
    """
    logger.info("📋 EHS 定期简报任务启动...")

    # TODO: 根据业务需求扩展以下流程：
    # 1. 从知识库拉取最新 EHS 记录
    # 2. 调用 LLM 生成合规摘要 / 安全周报
    # 3. 邮件推送
    # 4. Telegram 广播

    report_content = (
        f"# EHS 定期简报 | {datetime.now().strftime('%Y-%m-%d')}\n\n"
        f"本简报由 EHS Bot 自动生成。\n\n"
        f"（当前为占位模板，请在 ehs_bot/daily_job.py 中实现具体业务逻辑。）"
    )

    # 归档到知识库
    KB_DIR.mkdir(parents=True, exist_ok=True)
    file_name = f"EHS简报_{datetime.now().strftime('%Y%m%d')}.md"
    with open(KB_DIR / file_name, 'w', encoding='utf-8') as f:
        f.write(report_content)

    logger.info(f"💾 简报已归档：{file_name}")

    # 邮件推送
    subject = f"EHS 简报 | {datetime.now().strftime('%Y-%m-%d')}"
    try:
        send_market_report_email(subject, report_content)
        logger.info("📧 邮件推送成功")
    except Exception as e:
        logger.warning(f"❌ 邮件推送失败：{type(e).__name__} - {e}")

    # Telegram 广播
    try:
        from core.tg_base import broadcast_to_telegram
        _bot_token = os.getenv("EHS_TG_BOT_TOKEN", "")
        _user_ids = [
            int(u.strip()) for u in os.getenv("EHS_ALLOWED_TG_USERS", "").split(",")
            if u.strip().isdigit()
        ]
        _sandbox = SANDBOX_DIR
        if _bot_token and _user_ids:
            asyncio.run(broadcast_to_telegram(report_content, _bot_token, _user_ids, _sandbox))
            logger.info("📱 Telegram 广播成功")
    except Exception as e:
        logger.warning(f"❌ Telegram 广播失败：{e}")

    logger.info("✅ EHS 定期简报任务执行完毕")

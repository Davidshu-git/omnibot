"""
异步任务系统工具工厂 - trigger_job / query_job_status。
"""
import sys
import json
import uuid
import subprocess
from pathlib import Path
from datetime import datetime
from langchain_core.tools import tool

import logging

logger = logging.getLogger(__name__)


def make_job_tools(job_module: str, job_func: str = "job_routine") -> list:
    """
    创建异步任务触发与查询工具。

    Args:
        job_module: 任务函数所在的模块路径（如 'stock_bot.daily_job'）
        job_func:   任务函数名，默认 'job_routine'
    """

    @tool
    def trigger_job() -> str:
        """
        🚨【任务触发专用指令】：
        当用户明确要求触发后台任务时调用此工具。
        此工具会将任务投递至独立进程异步执行，调用后告知用户任务已挂载。

        Returns:
            str: 任务提交结果，包含任务 ID 用于追踪
        """
        try:
            job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

            status_dir = Path("./jobs/status").resolve()
            status_dir.mkdir(parents=True, exist_ok=True)

            initial_status = {
                "job_id": job_id,
                "status": "pending",
                "created_at": datetime.now().isoformat(),
                "started_at": None,
                "completed_at": None,
                "error": None,
                "log_path": None
            }
            with open(status_dir / f"{job_id}.json", 'w', encoding='utf-8') as f:
                json.dump(initial_status, f, ensure_ascii=False, indent=2)

            log_dir = Path("./jobs/logs").resolve()
            log_dir.mkdir(parents=True, exist_ok=True)
            stderr_log = open(log_dir / f"{job_id}_stderr.log", 'w', encoding='utf-8')

            process = subprocess.Popen(
                [sys.executable, "-m", "core.job_runner",
                 "--job-id", job_id,
                 "--job-module", job_module,
                 "--job-func", job_func],
                stdout=subprocess.DEVNULL,
                stderr=stderr_log,
                start_new_session=True
            )
            stderr_log.close()

            logger.info(f"任务进程已启动 | PID: {process.pid} | Job ID: {job_id}")

            return (
                f"✅ 任务已成功挂载至后台独立进程！\n\n"
                f"**任务 ID**: <code>{job_id}</code>\n\n"
                f"大约 3~4 分钟后任务完成，结果将自动推送。"
            )
        except Exception as e:
            return f"❌ 触发任务失败：{type(e).__name__} - {str(e)}"

    @tool
    def query_job_status(job_id: str) -> str:
        """
        查询后台任务的执行状态。

        Args:
            job_id: 任务唯一 ID（格式：job_YYYYMMDD_HHMMSS_abc123）
        """
        try:
            status_file = Path(f"./jobs/status/{job_id}.json").resolve()
            if not status_file.exists():
                return f"❌ 未找到任务 {job_id}，请检查任务 ID 是否正确。"

            with open(status_file, 'r', encoding='utf-8') as f:
                status = json.load(f)

            status_map = {
                "pending": "⏳ 等待执行",
                "running": "🔄 正在执行中",
                "completed": "✅ 已完成",
                "failed": "❌ 执行失败"
            }

            result = [
                f"📊 **任务状态报告**",
                f"- **任务 ID**: `{job_id}`",
                f"- **当前状态**: {status_map.get(status.get('status'), '未知')}",
                f"- **创建时间**: {status.get('created_at', 'N/A')}",
            ]
            if status.get('started_at'):
                result.append(f"- **开始时间**: {status.get('started_at')}")
            if status.get('completed_at'):
                result.append(f"- **完成时间**: {status.get('completed_at')}")
            if status.get('error'):
                result.append(f"- **错误信息**: {status.get('error')}")

            return "\n".join(result)
        except Exception as e:
            return f"❌ 查询失败：{type(e).__name__} - {str(e)}"

    return [trigger_job, query_job_status]

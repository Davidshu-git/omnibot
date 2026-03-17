#!/usr/bin/env python3
"""
通用独立进程启动器 - 供各 bot 的 trigger_job 工具跨进程调用。

使用方式:
    python -m core.job_runner --job-id job_xxx --job-module stock_bot.daily_job
"""
import sys
import json
import logging
import argparse
import importlib
from pathlib import Path
from datetime import datetime
from typing import TypedDict, Literal

JobStatus = Literal["pending", "running", "completed", "failed"]


class JobState(TypedDict, total=False):
    job_id: str
    status: JobStatus
    created_at: str
    started_at: str | None
    completed_at: str | None
    error: str | None
    log_path: str | None


def update_status(job_id: str, status: JobStatus, **kwargs) -> None:
    """原子更新任务状态文件"""
    status_dir = Path("./jobs/status").resolve()
    status_dir.mkdir(parents=True, exist_ok=True)
    status_file = status_dir / f"{job_id}.json"

    existing_data = {}
    if status_file.exists():
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    merged_data: JobState = {
        "job_id": job_id,
        **existing_data,
        "status": status,
        **kwargs
    }

    with open(status_file, 'w', encoding='utf-8') as f:
        json.dump(merged_data, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="通用任务独立进程启动器")
    parser.add_argument("--job-id", required=True, help="任务唯一 ID")
    parser.add_argument("--job-module", required=True, help="任务模块路径，如 stock_bot.daily_job")
    parser.add_argument("--job-func", default="job_routine", help="任务函数名，默认 job_routine")
    args = parser.parse_args()

    job_id = args.job_id

    log_dir = Path("./jobs/logs").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{job_id}.log"

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_path, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logger = logging.getLogger("job_runner")

    logger.info("=" * 60)
    logger.info(f"🚀 独立进程启动 | Job ID: {job_id} | PID: {__import__('os').getpid()}")
    logger.info(f"模块: {args.job_module}.{args.job_func} | 日志: {log_path}")
    logger.info("=" * 60)

    update_status(job_id=job_id, status="running",
                  started_at=datetime.now().isoformat(), log_path=str(log_path))

    try:
        module = importlib.import_module(args.job_module)
        job_func = getattr(module, args.job_func)
        logger.info(f"开始执行 {args.job_module}.{args.job_func}()...")
        job_func()

        update_status(job_id=job_id, status="completed",
                      completed_at=datetime.now().isoformat())
        logger.info(f"✅ 任务执行完成 | Job ID: {job_id}")

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        update_status(job_id=job_id, status="failed",
                      completed_at=datetime.now().isoformat(), error=error_msg)
        logger.exception(f"❌ 任务执行失败 | {error_msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()

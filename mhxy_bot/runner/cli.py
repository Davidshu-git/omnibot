"""CLI：按 JSON 任务定义逐个实例顺序执行游戏自动化。

用法：
    python -m mhxy_bot.runner.cli --task mijing --port 5557 --dry-run
    python -m mhxy_bot.runner.cli --task mijing --port 5557 --max-rounds 3
    python -m mhxy_bot.runner.cli --task mijing --all --max-rounds 3
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_TASKS_DIR = Path(__file__).parent.parent / "tasks"
_INSTANCES_PATH = Path(__file__).parent.parent.parent / "data" / "mhxy" / "config" / "instances.json"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_plan(task_def, ports: list[str], max_rounds: int) -> None:
    print(f"\n[DRY RUN] task={task_def.id}  name={task_def.name}  mode=single  max_rounds={max_rounds}")
    print(f"instances ({len(ports)}): {ports}")
    if task_def.preflight:
        print(f"preflight ({len(task_def.preflight)}):")
        for p in task_def.preflight:
            print(f"  [{p.id}] {p.action}  retries={p.retries}")
    print(f"steps ({len(task_def.steps)}):")
    for s in task_def.steps:
        line = f"  [{s.id}] {s.action}"
        if s.element:
            line += f"  element={s.element}"
        if s.text_any:
            line += f"  text_any={s.text_any}"
        if s.timeout_sec != 30:
            line += f"  timeout={s.timeout_sec}s"
        line += f"  retries={s.retries}"
        if s.verify_text_any:
            line += f"  verify={s.verify_text_any}"
        if s.verify_not_text_any:
            line += f"  verify_not={s.verify_not_text_any}"
        print(line)
    print()


def _run_instance(ctx, task_def, max_rounds: int, port: str) -> list[dict]:
    """最多 max_rounds 轮主任务，preflight 由 TaskEngine 按任务定义执行。"""
    from mhxy_bot.runner.engine import TaskEngine
    from mhxy_bot.runner.models import TaskStatus

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
        if result.step_results:
            failed = [
                {"step": sr.step_id, "message": sr.message}
                for sr in result.step_results
                if sr.message
            ]
            if failed:
                entry["step_details"] = failed
        results.append(entry)

        if result.status in (TaskStatus.NEEDS_HUMAN, TaskStatus.FAILED, TaskStatus.STOPPED):
            ctx.warning("port=%s round %d halted: status=%s step=%s msg=%s",
                        port, rnd, result.status.value, result.failed_step, result.message)
            break

    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m mhxy_bot.runner.cli",
        description="mhxy_bot 单人任务执行器（逐实例顺序执行）",
    )
    parser.add_argument("--task", required=True,
                        help="任务 ID（mhxy_bot/tasks/ 下 JSON 文件名，不含 .json）")
    parser.add_argument("--port", type=str, default=None,
                        help="仅执行单个端口")
    parser.add_argument("--all", dest="run_all", action="store_true",
                        help="对 instances.json 中所有实例逐个顺序执行")
    parser.add_argument("--max-rounds", type=int, default=None,
                        help="最大轮数（覆盖任务 JSON 中的 max_rounds）")
    parser.add_argument("--dry-run", action="store_true",
                        help="打印执行计划，不实际调用 executor")
    parser.add_argument("--executor-url", default=None,
                        help="Windows 执行器 URL（默认读 MHXY_EXECUTOR_URL 环境变量）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    # 加载任务
    task_path = _TASKS_DIR / f"{args.task}.json"
    if not task_path.exists():
        print(f"ERROR: 任务文件不存在: {task_path}", file=sys.stderr)
        sys.exit(1)

    from mhxy_bot.runner.models import TaskDefinition
    task_def = TaskDefinition.load(task_path)

    max_rounds = args.max_rounds if args.max_rounds is not None else int(task_def.meta.get("max_rounds", 3))

    # 确定端口列表
    if args.port and args.run_all:
        print("ERROR: --port 和 --all 不能同时使用", file=sys.stderr)
        sys.exit(1)
    if not args.port and not args.run_all:
        print("ERROR: 必须指定 --port <端口> 或 --all", file=sys.stderr)
        sys.exit(1)

    from mhxy_bot.runner.task_loader import build_context, get_all_ports, make_executor

    ports = [args.port] if args.port else get_all_ports(_INSTANCES_PATH)

    if args.dry_run:
        _print_plan(task_def, ports, max_rounds)
        return

    executor = make_executor(args.executor_url)
    all_results: list[dict] = []

    for port in ports:
        ctx = build_context(port, executor, dry_run=False)
        port_results = _run_instance(ctx, task_def, max_rounds, port)
        all_results.extend(port_results)

    print(json.dumps(all_results, ensure_ascii=False, indent=2))

    has_failure = any(r["status"] in ("failed", "needs_human") for r in all_results)
    sys.exit(1 if has_failure else 0)


if __name__ == "__main__":
    main()

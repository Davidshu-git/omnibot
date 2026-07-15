"""一次性迁移：把历史自由文本入金/出金流水结构化，点亮账户口径 MWR。

背景：``record_trade`` 支持出入金结构化前，早期资金进出以自由文本 ``details`` 记录,
缺 ``total_amount``/``currency`` 字段。[[mwr_engine]] 的账户口径 MWR 依赖**完整、精确**
的外部现金流(漏一笔则 Modified Dietz 全局失真),故对这些历史行做一次性结构化回填。

映射由 dawei 2026-07-15 逐笔核对确认(见对话对账表),**硬编码**而非脆弱正则解析:

- ``转账 6,400``(06-26)：dawei 确认「没转过 6400，记录错误」→ **删除**该行。
- ``转出 4,200``(07-01)：dawei 确认「提出用掉了」→ 改 ``action=出金``、4200 HKD。
- 其余 11 笔入金/出金：补 ``total_amount`` + ``currency``(原生币种)。

安全措施：默认 **dry-run** 只打印 diff；``--apply`` 才写盘,且先备份原文件为
``transaction_logs.jsonl.bak.<时间戳>``。回填行打 ``structured_backfill=True`` 溯源。

用法::

    # 预览(不写盘)
    .venv/bin/python -m stock_bot.backfill_cashflow_structuring
    # 实际执行(备份 + 写盘)
    .venv/bin/python -m stock_bot.backfill_cashflow_structuring --apply
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
TRANSACTION_LOG_FILE = BASE_DIR / "data/stock/memory/transaction_logs.jsonl"

# 特判哨兵:该 timestamp 的行整条删除(记录错误)。
_DELETE = "__DELETE__"

# 逐笔回填映射:timestamp(float,唯一) → 目标结构。``None`` 值表示删除该行。
# 浮点 timestamp 直接作 key 依赖 JSON 往返的位表示稳定性,匹配时用容差比对兜底。
BACKFILL: Dict[float, Optional[Dict[str, Any]]] = {
    1782456136.0282547: None,  # 转账 6400 → 删除(记录错误)
    1782867793.3041792: {"action": "出金", "total_amount": 4200.00, "currency": "HKD"},  # 转出→出金
    1782878081.6591134: {"action": "入金", "total_amount": 146.10, "currency": "USD"},
    1783207194.8269835: {"action": "入金", "total_amount": 1153.84, "currency": "HKD"},
    1783395847.690869:  {"action": "入金", "total_amount": 1.00, "currency": "USD"},
    1783508270.792418:  {"action": "入金", "total_amount": 20.00, "currency": "USD"},
    1783698113.3103633: {"action": "入金", "total_amount": 198.00, "currency": "USD"},
    1783700557.7170746: {"action": "入金", "total_amount": 1122.20, "currency": "USD"},
    1783702707.678586:  {"action": "出金", "total_amount": 4010.00, "currency": "HKD"},
    1783702708.2162635: {"action": "入金", "total_amount": 512.20, "currency": "USD"},
    1783702858.3743927: {"action": "入金", "total_amount": 2.00, "currency": "USD"},
    1783917702.9352784: {"action": "入金", "total_amount": 301.00, "currency": "HKD"},
    1783917702.9478402: {"action": "入金", "total_amount": 2.00, "currency": "USD"},
}


def _match_key(ts: Any) -> Optional[float]:
    """在 BACKFILL 中定位与行 timestamp 对应的 key(容差 1e-3 兜底浮点误差)。"""
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    if ts in BACKFILL:
        return ts
    for k in BACKFILL:
        if abs(k - ts) < 1e-3:
            return k
    return None


def _load_rows() -> List[Dict[str, Any]]:
    """读取全量流水行(坏行跳过);文件缺失抛出异常。"""
    rows: List[Dict[str, Any]] = []
    with open(TRANSACTION_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def plan(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """产出回填后的行列表(删除项剔除),不写盘。

    Args:
        rows: 原始流水行列表。

    Returns:
        List[Dict[str, Any]]: 应用回填后的行列表。
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        key = _match_key(row.get("timestamp"))
        if key is None:
            out.append(row)
            continue
        target = BACKFILL[key]
        if target is None:
            continue  # 删除该行
        new_row = dict(row)
        new_row["action"] = target["action"]
        new_row["total_amount"] = target["total_amount"]
        new_row["currency"] = target["currency"]
        new_row["structured_backfill"] = True
        out.append(new_row)
    return out


def _fmt(row: Dict[str, Any]) -> str:
    d = datetime.fromtimestamp(row["timestamp"]).strftime("%m-%d %H:%M")
    return (
        f"{d} {row.get('action'):<4} "
        f"amt={row.get('total_amount')} {row.get('currency') or ''}"
    )


def main(apply: bool) -> int:
    """执行回填(dry-run 或写盘)。

    Args:
        apply: True 则备份并写盘;False 仅打印 diff。

    Returns:
        int: 进程退出码(0 成功)。
    """
    if not TRANSACTION_LOG_FILE.is_file():
        print(f"❌ 流水文件不存在：{TRANSACTION_LOG_FILE}")
        return 1
    rows = _load_rows()
    new_rows = plan(rows)

    print(f"原始 {len(rows)} 行 → 回填后 {len(new_rows)} 行"
          f"（删除 {len(rows) - len(new_rows)} 行记录错误）\n")
    print("受影响行（回填 / 删除）：")
    for row in rows:
        key = _match_key(row.get("timestamp"))
        if key is None:
            continue
        d = datetime.fromtimestamp(row["timestamp"]).strftime("%m-%d %H:%M")
        det = str(row.get("details", ""))[:40]
        if BACKFILL[key] is None:
            print(f"  🗑  {d} {row.get('action')} 删除  | {det}")
        else:
            t = BACKFILL[key]
            print(f"  ✏  {d} {row.get('action')}→{t['action']} "
                  f"{t['total_amount']} {t['currency']}  | {det}")

    if not apply:
        print("\n[dry-run] 未写盘。确认无误后加 --apply 执行。")
        return 0

    backup = TRANSACTION_LOG_FILE.with_suffix(
        f".jsonl.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy2(TRANSACTION_LOG_FILE, backup)
    with open(TRANSACTION_LOG_FILE, "w", encoding="utf-8") as f:
        for row in new_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\n✅ 已写盘。备份：{backup.name}")
    print("下一步：重新落一次快照即可点亮 MWR（obs 面板『⟳ 重新估值』或等 16:30 盘后）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))

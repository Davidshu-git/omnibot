"""自选观察清单存储（投资总控台「👀 观察清单」数据地基）。

与持仓 / 估值引擎**彻底解耦**：观察清单是 0 持仓的纯跟踪位，只记
``{ticker, note, added_at}``，**绝不**参与任何财务计算，也绝不进 ``user_profile.json``
的持仓解析链路（那条链路的正则强依赖 "X 股，成本 Y" 格式，塞进观察位会被静默漏算
或污染净值）。

落盘路径：``data/stock/memory/watchlist.json``（一个 JSON 数组）。obs-api 已以只读方式
挂载 ``data/stock`` 到 ``/runtime/stock-bot``，故观察清单的**读取**由 obs 侧直接读文件
（同 portfolio 快照范式，不依赖 live bot）；**增删**为写操作，obs 只读，必须委托 live bot
（本模块 + tg_main 钩子 + obs_action 端点）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 软上限：观察清单是"快速浏览关注公司"的轻量列表，不是股票池，超过即拒绝新增，
# 避免无界膨胀拖慢页面。真要大批量跟踪应该用「选股」页的股票池。
MAX_WATCHLIST_SIZE: int = 100


def _watchlist_path(memory_dir: Path) -> Path:
    return memory_dir / "watchlist.json"


def load_watchlist(memory_dir: Path) -> List[Dict[str, Any]]:
    """读取观察清单，文件不存在或损坏返回空列表（不抛异常，供首次使用时优雅降级）。

    Args:
        memory_dir: 记忆目录（``data/stock/memory``）。

    Returns:
        List[Dict[str, Any]]: 观察条目列表 ``[{"ticker", "note", "added_at"}]``；
            按新增时间倒序（新加的在前）。
    """
    path = _watchlist_path(memory_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("[watchlist] 读取观察清单失败：%s", path)
        return []
    return data if isinstance(data, list) else []


def _write_watchlist(memory_dir: Path, items: List[Dict[str, Any]]) -> None:
    """原子写回观察清单（tmp + replace，避免半写文件被并发读到）。"""
    path = _watchlist_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_to_watchlist(memory_dir: Path, ticker: str, note: str = "") -> Dict[str, Any]:
    """新增一个观察标的（按代码大小写不敏感去重；已存在则仅更新备注）。

    Args:
        memory_dir: 记忆目录（``data/stock/memory``）。
        ticker: 股票 / 加密货币代码（原始输入即可，如 ``AAPL`` / ``0700.HK`` / ``BTC``）；
            存储时按首尾空白清理，去重按 upper() 归一。
        note: 可选备注（如"等回踩 MA60"）。

    Returns:
        Dict[str, Any]: ``{"status": "ok"|"exists"|"invalid"|"full", "items": [...]}``。
            ``exists`` 表示已在清单中（已更新备注），``full`` 表示达到上限拒绝新增。
    """
    clean = ticker.strip()
    if not clean:
        return {"status": "invalid", "items": load_watchlist(memory_dir)}

    items = load_watchlist(memory_dir)
    key = clean.upper()
    for it in items:
        if str(it.get("ticker", "")).strip().upper() == key:
            # 已存在：仅在传入了新备注时更新，不改 added_at、不重排。
            if note.strip():
                it["note"] = note.strip()
                _write_watchlist(memory_dir, items)
            return {"status": "exists", "items": items}

    if len(items) >= MAX_WATCHLIST_SIZE:
        return {"status": "full", "items": items}

    entry = {
        "ticker": clean,
        "note": note.strip(),
        "added_at": datetime.now().strftime("%Y-%m-%d"),
    }
    items.insert(0, entry)  # 新加的排在最前，方便"快速浏览最近关注的"
    _write_watchlist(memory_dir, items)
    return {"status": "ok", "items": items}


def remove_from_watchlist(memory_dir: Path, ticker: str) -> Dict[str, Any]:
    """从观察清单移除一个标的（按代码大小写不敏感匹配）。

    Args:
        memory_dir: 记忆目录（``data/stock/memory``）。
        ticker: 要移除的代码。

    Returns:
        Dict[str, Any]: ``{"status": "ok"|"not_found", "items": [...]}``。
    """
    key = ticker.strip().upper()
    if not key:
        return {"status": "not_found", "items": load_watchlist(memory_dir)}

    items = load_watchlist(memory_dir)
    kept = [it for it in items if str(it.get("ticker", "")).strip().upper() != key]
    if len(kept) == len(items):
        return {"status": "not_found", "items": items}
    _write_watchlist(memory_dir, kept)
    return {"status": "ok", "items": kept}

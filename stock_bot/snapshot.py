"""投资组合快照器（投资总控台数据地基）。

复用 ``valuation_engine`` 这一唯一财务计算来源，把组合估值结果落盘成
时间序列 JSONL（每日一行），供 obs「投资总控台」只读消费。本模块**绝不**
自己取价或重算盈亏，只做"调用引擎 + 派生汇总 + 落盘"。

落盘路径：``data/stock/snapshots/portfolio.jsonl``。obs-api 已以只读方式挂载
``data/stock`` 到容器内 ``/runtime/stock-bot``，故 obs 侧路径为
``/runtime/stock-bot/snapshots/portfolio.jsonl``，无需改 compose。

用法：
    # 手动生成一次（盘中按需 / 调试）
    python -m stock_bot.snapshot

    # 在 daily_job 盘后流程内调用 write_snapshot(valuation)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from stock_bot.valuation_engine import (
    calculate_portfolio_valuation,
    fetch_fx_trend,
    parse_cash_assets,
    parse_user_profile_to_positions,
)

logger = logging.getLogger(__name__)

BASE_DIR: Path = Path(__file__).resolve().parent.parent
STOCK_MEMORY_DIR: Path = BASE_DIR / "data/stock/memory"
SNAPSHOT_DIR: Path = BASE_DIR / "data/stock/snapshots"
SNAPSHOT_FILE: Path = SNAPSHOT_DIR / "portfolio.jsonl"


def _load_user_profile() -> Dict[str, Any]:
    """读取 user_profile.json（持仓 + 现金 + 偏好）。

    Returns:
        Dict[str, Any]: 用户记忆字典；文件缺失或解析失败时返回空字典。
    """
    profile_path: Path = STOCK_MEMORY_DIR / "user_profile.json"
    if not profile_path.exists():
        return {}
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取 user_profile.json 失败：%s", exc)
        return {}


def build_snapshot(valuation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构建一条组合快照（含派生汇总字段）。

    Args:
        valuation: 已算好的 ``calculate_portfolio_valuation`` 结果。为 None 时
            本函数从 user_profile.json 现算一次（手动 / 盘中按需路径）。

    Returns:
        Dict[str, Any]: 快照字典，关键字段：

            - ``date`` / ``generated_at``：日期与生成时刻
            - ``total_market_value`` / ``total_cost`` / ``total_profit_loss``
              / ``profit_loss_percent``：组合整体（CNY 口径，含现金）
            - ``securities_total_cny`` / ``cash_total_cny``：证券 vs 现金拆分
            - ``currency_exposure``：按币种折 CNY 的敞口（证券 + 现金）
            - ``holdings`` / ``cash_holdings`` / ``exchange_rates``：明细透传
    """
    if valuation is None:
        user_data = _load_user_profile()
        positions = parse_user_profile_to_positions(user_data)
        cash_assets = parse_cash_assets(user_data)
        valuation = calculate_portfolio_valuation(positions, cash_assets)

    holdings: List[Dict[str, Any]] = valuation.get("holdings", [])
    cash_holdings: List[Dict[str, Any]] = valuation.get("cash_holdings", [])
    cash_total_cny: float = valuation.get("cash_total_cny", 0.0)

    # 资产分类汇总（均仅统计估值成功的持仓）：
    #   - 证券 = type≠crypto 的持仓（股票/ETF），加密货币单列，不再混入证券；
    #   - 加密 = type==crypto 的持仓（BTC/ETH 等）。
    # 历史快照无 crypto_total_cny 字段，obs 前端须 ?? 0 兜底。
    securities_total_cny: float = round(
        sum(
            h.get("market_value_cny", 0.0)
            for h in holdings
            if "error" not in h and h.get("type") != "crypto"
        ), 2
    )
    crypto_total_cny: float = round(
        sum(
            h.get("market_value_cny", 0.0)
            for h in holdings
            if "error" not in h and h.get("type") == "crypto"
        ), 2
    )

    # 币种敞口（CNY 口径，仅法币）：证券按持仓 currency 归并 + 现金按 currency 归并。
    # 加密货币（type==crypto）虽以美元报价，但其波动主要来自币市本身而非汇率，
    # 计入美元敞口会高估真实法币汇率风险，故剔除——加密风险由「加密货币」组单独体现。
    # 故 currency_exposure 合计 = 总净值 − 加密市值（仅法币口径）。
    currency_exposure: Dict[str, float] = {}
    for h in holdings:
        if "error" in h or h.get("type") == "crypto":
            continue
        cur = h.get("currency", "CNY")
        currency_exposure[cur] = currency_exposure.get(cur, 0.0) + h.get("market_value_cny", 0.0)
    for c in cash_holdings:
        cur = c.get("currency", "CNY")
        currency_exposure[cur] = currency_exposure.get(cur, 0.0) + c.get("cny_value", 0.0)
    currency_exposure = {k: round(v, 2) for k, v in currency_exposure.items()}

    # 汇率趋势（非关键增益）：仅对敞口里出现的非 CNY 币种取近期走势，供 obs 展示。
    # fetch_fx_trend 内部已对每币种做异常降级，失败返回空 dict，绝不打断快照。
    fx_currencies = [c for c in currency_exposure if c != "CNY"]
    fx_trend = fetch_fx_trend(fx_currencies) if fx_currencies else {}

    now = datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "currency_unit": "CNY",
        "total_market_value": valuation.get("total_market_value", 0.0),
        "total_cost": valuation.get("total_cost", 0.0),
        "total_profit_loss": valuation.get("total_profit_loss", 0.0),
        "profit_loss_percent": valuation.get("profit_loss_percent", 0.0),
        "securities_total_cny": securities_total_cny,
        "crypto_total_cny": crypto_total_cny,
        "cash_total_cny": cash_total_cny,
        "currency_exposure": currency_exposure,
        "fx_trend": fx_trend,
        "holdings": holdings,
        "cash_holdings": cash_holdings,
        "exchange_rates": valuation.get("exchange_rates", {}),
    }


def write_snapshot(valuation: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """生成并落盘一条快照（同日去重：覆盖当天最后一条）。

    JSONL 每行一条快照。若当天已有快照，则**替换**为最新一条而非追加，
    保证"每日一行"语义，避免盘中多次手动刷新把同一天写成几十行。

    Args:
        valuation: 已算好的估值结果；为 None 则内部现算（见 ``build_snapshot``）。

    Returns:
        Optional[Dict[str, Any]]: 写入的快照字典；落盘失败返回 None。
    """
    try:
        snapshot = build_snapshot(valuation)
    except Exception as exc:  # 估值引擎异常不可冒泡到 daily_job 主流程
        logger.error("构建组合快照失败：%s - %s", type(exc).__name__, exc)
        return None

    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        # 读出历史，剔除同日旧条目，再追加最新条目，整体回写
        rows: List[Dict[str, Any]] = []
        if SNAPSHOT_FILE.exists():
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 跳过坏行，不让一行损坏拖垮整个文件
                    if row.get("date") != snapshot["date"]:
                        rows.append(row)
        rows.append(snapshot)
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info(
            "组合快照已落盘：%s 总净值 ¥%.2f（证券 ¥%.2f + 现金 ¥%.2f）",
            snapshot["date"], snapshot["total_market_value"],
            snapshot["securities_total_cny"], snapshot["cash_total_cny"],
        )
        return snapshot
    except OSError as exc:
        logger.error("组合快照落盘失败：%s", exc)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    snap = write_snapshot()
    if snap is None:
        print("快照生成失败，详见日志。")
    else:
        print(json.dumps(snap, ensure_ascii=False, indent=2))

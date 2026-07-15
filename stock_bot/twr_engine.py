"""证券投资 TWR（时间加权收益率）引擎——投资总控台「MTD/YTD/1Y」点亮的算力地基。

口径（与 dawei 2026-07-14 决策一致）：**证券投资口径**，只对股票/ETF/加密货币算
真实时间加权收益，现金不进收益率分母。

- 价值序列：每条快照的 ``securities_total_cny + crypto_total_cny``（引擎已折 CNY）。
- 现金流：``record_trade`` 落下的**结构化**买/卖流水（买入 = 资金流入证券组合 +，
  卖出 = 流出 −），用**当日快照汇率**把原生币种折 CNY。入金/出金/转账**不碰**——
  它们是现金账户内部搬钱，不改变证券市值，天然与本口径无关。

为什么必须做「现金流可信起点」门控：早期流水（``append_transaction_log`` 自由文本
或 record_trade 上线前）缺 ``total_amount``/``currency`` 结构化字段，无法可靠折算。
若静默丢弃这些买入，会把「加仓推高的市值」错算成「投资收益」——正是 CLAUDE.md
「严禁用净值日间差冒充收益率」红线。故：**只有基准快照日 ≥ 最后一笔未结构化流水日
的窗口才可信**，其余窗口标 ``twr_available=False`` 并给出原因，随数据积累自动点亮。

本模块**不取价、不重算估值**，只对引擎已算好的 CNY 值做时间加权链接，是纯函数。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 各窗口显示名。今日/累计同时是「净值走势」窗（前端沿用），此处一并给出 TWR。
WINDOW_NAMES: Tuple[str, ...] = ("今日", "WTD", "MTD", "YTD", "1Y", "累计")

# 单日「隐含市场收益」合理上限。证券+加密的分散组合，扣掉当日现金流后仍出现单日
# ±25% 以上的隐含涨跌，几乎必然是**数据不自洽**（如持仓被整只删除却无对应卖出，
# 或快照与流水时序错配），而非真实行情。命中即判该窗 TWR 不可信——宁可灰置也不
# 把凭空蒸发的持仓算成"暴亏"。背景：2026-07-09 VOO 整只从档案消失但仅记了部分卖出，
# 使日链 TWR 砸出假的 −27%。
_MAX_SANE_DAILY_RETURN: float = 0.25


def _parse_date(s: Any) -> Optional[date]:
    """把 ``YYYY-MM-DD`` 字符串解析为 date；非法返回 None。"""
    if not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _invested_value(snap: Dict[str, Any]) -> float:
    """一条快照的证券投资市值（证券 + 加密，均为 CNY，不含现金）。"""
    return float(snap.get("securities_total_cny", 0.0) or 0.0) + float(
        snap.get("crypto_total_cny", 0.0) or 0.0
    )


def _has_pricing_error(snap: Dict[str, Any]) -> bool:
    """判定快照是否含取价失败——**不只信顶层 has_pricing_error 标志**。

    历史快照存在顶层标志未置位、但某持仓 ``error`` 为真的不一致（如 2026-06-29 BTC
    取价失败却 has_pricing_error=None）。此时该标的会从证券总额中凭空消失、次日复现，
    在 TWR 序列里制造假摆动。故逐持仓兜底检查，任一持仓有 error 即视整条快照不可用。
    """
    if snap.get("has_pricing_error"):
        return True
    for h in snap.get("holdings", []):
        if isinstance(h, dict) and h.get("error"):
            return True
    return False


def _window_start(name: str, as_of: date, first_snap: date) -> Optional[date]:
    """计算窗口起点日期。

    Args:
        name: 窗口名（见 ``WINDOW_NAMES``）。
        as_of: 计算基准「今天」。
        first_snap: 最早一条快照日期（累计窗起点）。

    Returns:
        窗口起点日期；未知窗口名返回 None。
    """
    if name == "今日":
        return as_of - timedelta(days=1)
    if name == "WTD":  # 本周一（ISO：周一=1）
        return as_of - timedelta(days=as_of.isoweekday() - 1)
    if name == "MTD":
        return as_of.replace(day=1)
    if name == "YTD":
        return as_of.replace(month=1, day=1)
    if name == "1Y":
        return as_of - timedelta(days=365)
    if name == "累计":
        return first_snap
    return None


def _extract_flows(
    transactions: List[Dict[str, Any]],
    fx_by_date: Dict[date, Dict[str, float]],
    latest_fx: Dict[str, float],
) -> Tuple[Dict[date, float], Optional[date]]:
    """从交易流水提取每日 CNY 净现金流，并定位最后一笔未结构化流水日。

    只统计 action 为「买入/卖出」的行。结构化行须同时含 ``total_amount``（数值）与
    ``currency``；缺任一即视为「未结构化」——它标记该日为不可信，但**不参与**现金流累加
    （无法可靠折算，宁缺毋错）。

    折 CNY 汇率优先用**成交当日**快照的 ``exchange_rates``；当日无快照则用**最近一条
    快照**的汇率兜底（碎股金额小，误差可忽略）。

    Args:
        transactions: 交易流水行列表（每行含 timestamp/action/... ）。
        fx_by_date: ``{日期: {"USD_CNY": r, "HKD_CNY": r, ...}}``，来自各快照。
        latest_fx: 最近一条快照的汇率表，作为兜底。

    Returns:
        Tuple[Dict[date, float], Optional[date]]:
            ``(每日净流入CNY(买+卖-), 最后一笔未结构化流水日期或None)``。
    """
    flows: Dict[date, float] = {}
    last_unstructured: Optional[date] = None
    sorted_fx_dates = sorted(fx_by_date)

    for row in transactions:
        if not isinstance(row, dict):
            continue
        if row.get("action") not in ("买入", "卖出"):
            continue
        ts = row.get("timestamp")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        try:
            d = datetime.fromtimestamp(float(ts)).date()
        except (ValueError, OSError, OverflowError):
            continue

        amt = row.get("total_amount")
        cur = row.get("currency")
        structured = (
            isinstance(amt, (int, float))
            and not isinstance(amt, bool)
            and isinstance(cur, str)
            and cur
        )
        if not structured:
            if last_unstructured is None or d > last_unstructured:
                last_unstructured = d
            continue

        rate = _resolve_rate(str(cur), d, fx_by_date, sorted_fx_dates, latest_fx)
        cny = float(amt) * rate
        sign = 1.0 if row.get("action") == "买入" else -1.0
        flows[d] = flows.get(d, 0.0) + sign * cny

    return flows, last_unstructured


def _resolve_rate(
    currency: str,
    d: date,
    fx_by_date: Dict[date, Dict[str, float]],
    sorted_fx_dates: List[date],
    latest_fx: Dict[str, float],
) -> float:
    """定位某币种在某日折 CNY 的汇率：当日快照 → 最近快照 → 1.0（CNY）。"""
    if currency == "CNY":
        return 1.0
    key = f"{currency}_CNY"
    same_day = fx_by_date.get(d, {})
    if key in same_day and same_day[key]:
        return float(same_day[key])
    # 最近一条 ≤ d 的快照汇率；再退化为任意最近的
    for dd in reversed(sorted_fx_dates):
        if dd <= d and fx_by_date[dd].get(key):
            return float(fx_by_date[dd][key])
    for dd in reversed(sorted_fx_dates):
        if fx_by_date[dd].get(key):
            return float(fx_by_date[dd][key])
    if latest_fx.get(key):
        return float(latest_fx[key])
    logger.warning("无法定位 %s 在 %s 的汇率，按 1.0 折算（可能失真）", key, d)
    return 1.0


def _linked_twr(
    series: List[Tuple[date, float]],
    flows: Dict[date, float],
) -> Tuple[Optional[float], float]:
    """日链时间加权收益率 + 最大单日隐含收益（供一致性哨兵判定）。

    对基准之后的每一天 d（前一采样日 p）：
        ``r_d = (V(d) − F(d) − V(p)) / V(p)``
    其中 ``F(d)`` 为当日净现金流（买+卖−），按「期末流入」约定——当日流入不参与当日
    收益，故从 V(d) 减去，隔离出纯市场驱动的涨跌。几何链接：``TWR = ∏(1+r_d) − 1``。

    Args:
        series: ``[(日期, 证券市值CNY), ...]``，须按日期升序，长度 ≥ 2，起点为基准。
        flows: 每日净现金流 CNY。

    Returns:
        Tuple[Optional[float], float]: ``(TWR小数, 区间内最大 |单日隐含收益|)``。
        样本不足或基准值非正时 TWR 为 None、max 为 0.0。
    """
    if len(series) < 2:
        return None, 0.0
    cum = 1.0
    max_abs_daily = 0.0
    for i in range(1, len(series)):
        (_, v_prev) = series[i - 1]
        (d_cur, v_cur) = series[i]
        if v_prev <= 0:
            continue  # 空仓日无法计收益，跳过（几何链接乘 1）
        f = flows.get(d_cur, 0.0)
        r = (v_cur - f - v_prev) / v_prev
        max_abs_daily = max(max_abs_daily, abs(r))
        cum *= (1.0 + r)
    return cum - 1.0, max_abs_daily


def compute_windowed_returns(
    snapshots: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """计算各时间窗的证券投资 TWR 与可用性。

    Args:
        snapshots: 组合快照列表（每条含 date/securities_total_cny/crypto_total_cny/
            exchange_rates/has_pricing_error 等）。顺序不限，内部按 date 升序处理。
        transactions: 交易流水行列表（``transaction_logs.jsonl`` 解析结果）。
        as_of: 计算基准「今天」；默认取系统当天。

    Returns:
        Dict[str, Any]: ``returns.json`` 结构：

            - ``basis``: 固定 ``"securities"``（证券投资口径）
            - ``as_of_date`` / ``generated_at``
            - ``first_snapshot_date`` / ``flow_genesis_date``（现金流可信起点=最后一笔
              未结构化流水次日；无未结构化流水则为最早快照日）
            - ``windows``: ``{窗口名: {chart_available, twr_available, twr_pct,
              start_date, baseline_date, days, reason}}``
    """
    if as_of is None:
        as_of = date.today()

    # 取价异常的快照剔除（与 obs history 端点口径一致，避免单标的取价失败画假跳水）。
    clean: List[Tuple[date, Dict[str, Any]]] = []
    for s in snapshots:
        if not isinstance(s, dict) or _has_pricing_error(s):
            continue
        d = _parse_date(s.get("date"))
        if d is not None:
            clean.append((d, s))
    clean.sort(key=lambda t: t[0])

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if len(clean) < 2:
        # 数据不足：全窗不可用，但仍回结构，前端据此渲染灰态。
        windows = {
            name: {
                "chart_available": False,
                "twr_available": False,
                "twr_pct": None,
                "start_date": None,
                "baseline_date": None,
                "days": 0,
                "reason": "快照不足两天，待积累",
            }
            for name in WINDOW_NAMES
        }
        return {
            "basis": "securities",
            "as_of_date": as_of.strftime("%Y-%m-%d"),
            "generated_at": generated_at,
            "first_snapshot_date": clean[0][0].strftime("%Y-%m-%d") if clean else None,
            "flow_genesis_date": None,
            "windows": windows,
        }

    dates = [d for d, _ in clean]
    value_by_date: Dict[date, float] = {d: _invested_value(s) for d, s in clean}
    fx_by_date: Dict[date, Dict[str, float]] = {
        d: (s.get("exchange_rates") or {}) for d, s in clean
    }
    latest_fx = fx_by_date[dates[-1]]
    first_snap, latest_snap = dates[0], dates[-1]

    flows, last_unstructured = _extract_flows(transactions, fx_by_date, latest_fx)
    # 现金流可信起点：最后一笔未结构化流水的**次日**起，所有流水都结构化可信。
    # 无未结构化流水 → 从最早快照起全程可信。
    genesis = (last_unstructured + timedelta(days=1)) if last_unstructured else first_snap

    windows: Dict[str, Any] = {}
    for name in WINDOW_NAMES:
        windows[name] = _eval_window(
            name, as_of, first_snap, latest_snap, dates,
            value_by_date, flows, last_unstructured,
        )

    return {
        "basis": "securities",
        "as_of_date": as_of.strftime("%Y-%m-%d"),
        "generated_at": generated_at,
        "first_snapshot_date": first_snap.strftime("%Y-%m-%d"),
        "flow_genesis_date": genesis.strftime("%Y-%m-%d"),
        "windows": windows,
    }


def _eval_window(
    name: str,
    as_of: date,
    first_snap: date,
    latest_snap: date,
    dates: List[date],
    value_by_date: Dict[date, float],
    flows: Dict[date, float],
    last_unstructured: Optional[date],
) -> Dict[str, Any]:
    """评估单个窗口的可用性与 TWR。见 ``compute_windowed_returns`` 返回结构。"""
    start = _window_start(name, as_of, first_snap)
    out: Dict[str, Any] = {
        "chart_available": False,
        "twr_available": False,
        "twr_pct": None,
        "start_date": start.strftime("%Y-%m-%d") if start else None,
        "baseline_date": None,
        "days": 0,
        "reason": None,
    }
    if start is None:
        out["reason"] = "未知窗口"
        return out

    # 基准 = 最后一条 date ≤ start 的快照（窗口起点的真实净值锚点）。
    baseline: Optional[date] = None
    for d in dates:
        if d <= start:
            baseline = d
    if baseline is None:
        # 窗口起点早于全部快照——无真实基准（YTD/1Y 当前即此情形）。
        if name == "YTD":
            out["reason"] = f"需 {start.year} 年初基准，数据从 {first_snap} 起"
        elif name == "1Y":
            out["reason"] = "需满 12 个月数据"
        else:
            out["reason"] = f"窗口起点早于最早快照 {first_snap}"
        return out

    seq = [(d, value_by_date[d]) for d in dates if d >= baseline]
    out["chart_available"] = len(seq) >= 2
    out["baseline_date"] = baseline.strftime("%Y-%m-%d")
    out["days"] = (latest_snap - baseline).days
    if not out["chart_available"]:
        out["reason"] = "区间内快照不足两天"
        return out

    # TWR 可信门控：基准日须 ≥ 最后一笔未结构化流水日，否则区间内含无法折算的买卖，
    # 强算会把加仓污染成收益（红线）。此时图表可看净值，但 TWR 明确不可用。
    if last_unstructured is not None and baseline < last_unstructured:
        out["reason"] = f"投资收益待早期流水结构化（基准 {baseline} 早于可信起点）"
        return out

    twr, max_abs_daily = _linked_twr(seq, flows)
    if twr is None:
        out["reason"] = "样本不足，无法计算 TWR"
        return out
    # 一致性哨兵：区间内任一单日隐含收益超过合理上限，判定数据不自洽（持仓突变/时序
    # 错配），TWR 不可信。图表仍可看净值，但不给出会误导的收益率。
    if max_abs_daily > _MAX_SANE_DAILY_RETURN:
        out["reason"] = (
            f"区间含无法用交易解释的持仓突变（单日隐含 {max_abs_daily * 100:.0f}%），"
            f"收益率不可信"
        )
        return out
    out["twr_available"] = True
    out["twr_pct"] = round(twr * 100.0, 2)
    return out

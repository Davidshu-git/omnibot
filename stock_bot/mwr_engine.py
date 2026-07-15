"""账户口径 MWR(资金加权收益率)引擎——投资总控台「我掏了多少真金白银、现在值多少」。

口径（与 dawei 2026-07-15 决策一致）：**整体账户口径**,覆盖范围 = Neverless(美股) +
众安(港币)两个资金账户。与 [[twr_engine]] 的「证券组合口径」互补并列:

- 价值序列：每条快照的 ``total_market_value``(**总净值 = 证券 + 现金**,均已折 CNY)。
  与 TWR 只看证券市值不同——账户口径把闲置现金也计入分母,现金收益恒 0 会**稀释**
  收益率,这是账户口径的固有特性(回答的是「整个投资盘子每一块钱的效率」,而非
  「选股水平」)。
- 现金流：只认**外部资金进出**——``record_trade`` 落的 ``入金``(投资者掏钱,组合流入 +)
  与 ``出金``(投资者取回,组合流出 −)。**买入/卖出不算**——它们是账户内部现金↔证券
  的换手,不是新钱进出,这正是账户口径相对证券口径 TWR 的关键区别。**内部转账**
  (账户间搬钱)天然不入(record_trade 无「转账」action)。

主指标 **Modified Dietz**(GIPS 认可的区间累计 MWR 近似,非年化,无需迭代、数值稳定):

    R = (EMV − BMV − F_net) / (BMV + Σ w_i · F_i)

其中 EMV/BMV = 期末/期初总净值,F_i = 第 i 笔外部净流入(组合视角:入金 +、出金 −),
F_net = Σ F_i,权重 w_i = (T − t_i)/T(T = 区间天数,t_i = 该笔距期初天数)。

副指标 **年化 XIRR**(投资者视角现金流解 NPV=0):短窗(如仅数周)会被数学放大到
数百 %,**仅供参考、前端须标注**,主显 Modified Dietz 区间值。

为什么必须做「现金流可信起点」门控:早期入金/出金流水(record_trade 支持出入金前
或自由文本回填)缺 ``total_amount``/``currency`` 结构化字段,无法可靠折算。若静默漏
一笔外部流,Modified Dietz 分子/分母双错——比 TWR 更敏感(TWR 漏一笔只污染一段,
MWR 是全局比率、一笔错则整窗失真)。故:**只有基准快照日 ≥ 最后一笔未结构化外部流
日的窗口才可信**,其余窗口标 ``mwr_available=False`` 并给出原因,随数据积累自动点亮。

本模块**不取价、不重算估值**,只对引擎已算好的 CNY 总净值做资金加权,是纯函数。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 各窗口显示名。与 twr_engine.WINDOW_NAMES 对齐,保证前端两指标同窗并列。
WINDOW_NAMES: Tuple[str, ...] = ("今日", "WTD", "MTD", "YTD", "1Y", "累计")

# 账户口径区间累计 MWR 合理上限。含现金的总盘子在数周~数月窗口内出现 |R|>60% 的
# 资金加权收益,几乎必然是**数据不自洽**(外部流漏记/金额错/快照口径突变),而非真实
# 表现。命中即判该窗不可信——宁可灰置也不给误导性收益率。
_MAX_SANE_MWR: float = 0.60


def _parse_date(s: Any) -> Optional[date]:
    """把 ``YYYY-MM-DD`` 字符串解析为 date；非法返回 None。(与 twr_engine 同源)"""
    if not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _account_value(snap: Dict[str, Any]) -> float:
    """一条快照的账户总净值(证券 + 现金,CNY)。

    优先用 ``total_market_value``(build_snapshot 落的总净值);历史快照缺该字段时,
    退化为 ``securities_total_cny + crypto_total_cny + cash_total_cny`` 兜底求和。
    """
    tmv = snap.get("total_market_value")
    if isinstance(tmv, (int, float)) and not isinstance(tmv, bool):
        return float(tmv)
    return (
        float(snap.get("securities_total_cny", 0.0) or 0.0)
        + float(snap.get("crypto_total_cny", 0.0) or 0.0)
        + float(snap.get("cash_total_cny", 0.0) or 0.0)
    )


def _has_pricing_error(snap: Dict[str, Any]) -> bool:
    """判定快照是否含取价失败——逐持仓兜底,任一持仓 error 即视整条不可用。

    与 twr_engine 同源:历史快照存在顶层 ``has_pricing_error`` 未置位、但某持仓
    ``error`` 为真的不一致,该标的会从总额中凭空消失、次日复现,在 MWR 序列里制造假
    摆动。故不只信顶层标志。
    """
    if snap.get("has_pricing_error"):
        return True
    for h in snap.get("holdings", []):
        if isinstance(h, dict) and h.get("error"):
            return True
    return False


def _window_start(name: str, as_of: date, first_snap: date) -> Optional[date]:
    """计算窗口起点日期。(与 twr_engine 同源,保证两指标窗口口径一致)

    Args:
        name: 窗口名(见 ``WINDOW_NAMES``)。
        as_of: 计算基准「今天」。
        first_snap: 最早一条快照日期(累计窗起点)。

    Returns:
        窗口起点日期;未知窗口名返回 None。
    """
    if name == "今日":
        return as_of - timedelta(days=1)
    if name == "WTD":  # 本周一(ISO:周一=1)
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


def _resolve_rate(
    currency: str,
    d: date,
    fx_by_date: Dict[date, Dict[str, float]],
    sorted_fx_dates: List[date],
    latest_fx: Dict[str, float],
) -> float:
    """定位某币种在某日折 CNY 的汇率:当日快照 → 最近快照 → 1.0(CNY)。(与 twr_engine 同源)"""
    if currency == "CNY":
        return 1.0
    key = f"{currency}_CNY"
    same_day = fx_by_date.get(d, {})
    if key in same_day and same_day[key]:
        return float(same_day[key])
    for dd in reversed(sorted_fx_dates):
        if dd <= d and fx_by_date[dd].get(key):
            return float(fx_by_date[dd][key])
    for dd in reversed(sorted_fx_dates):
        if fx_by_date[dd].get(key):
            return float(fx_by_date[dd][key])
    if latest_fx.get(key):
        return float(latest_fx[key])
    logger.warning("无法定位 %s 在 %s 的汇率,按 1.0 折算(可能失真)", key, d)
    return 1.0


def _extract_external_flows(
    transactions: List[Dict[str, Any]],
    fx_by_date: Dict[date, Dict[str, float]],
    latest_fx: Dict[str, float],
) -> Tuple[List[Tuple[date, float]], Optional[date]]:
    """从交易流水提取外部现金流(入金/出金),折 CNY,并定位最后一笔未结构化流水日。

    只统计 action 为「入金/出金」的行(买卖是账户内换手,账户口径不算)。结构化行须同时
    含 ``total_amount``(数值)与 ``currency``;缺任一即「未结构化」——标记该日为不可信,但
    **不参与**现金流累加(无法可靠折算,宁缺毋错)。

    折 CNY 优先用**成交当日**快照汇率,当日无快照则用最近一条快照汇率兜底。

    Args:
        transactions: 交易流水行列表(每行含 timestamp/action/total_amount/currency)。
        fx_by_date: ``{日期: {"USD_CNY": r, ...}}``,来自各快照。
        latest_fx: 最近一条快照的汇率表,兜底。

    Returns:
        Tuple[List[Tuple[date, float]], Optional[date]]:
            ``([(日期, 组合视角净流入CNY:入金+/出金−), ...], 最后一笔未结构化流水日或None)``。
            列表按日期升序。
    """
    flows: List[Tuple[date, float]] = []
    last_unstructured: Optional[date] = None
    sorted_fx_dates = sorted(fx_by_date)

    for row in transactions:
        if not isinstance(row, dict):
            continue
        if row.get("action") not in ("入金", "出金"):
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
        # 组合视角:入金流入组合(+),出金流出组合(−)。
        sign = 1.0 if row.get("action") == "入金" else -1.0
        flows.append((d, sign * cny))

    flows.sort(key=lambda t: t[0])
    return flows, last_unstructured


def _modified_dietz(
    bmv: float,
    emv: float,
    flows: List[Tuple[date, float]],
    baseline: date,
    end: date,
) -> Optional[float]:
    """Modified Dietz 区间累计资金加权收益率(非年化)。

    R = (EMV − BMV − F_net) / (BMV + Σ w_i·F_i),w_i = (T − t_i)/T。
    仅计入 ``baseline < d ≤ end`` 的外部流(baseline 当日及之前的资金已含于 BMV)。

    Args:
        bmv: 期初总净值(baseline 日)。
        emv: 期末总净值(end 日)。
        flows: ``[(日期, 组合视角净流入CNY), ...]``,全量外部流(内部按窗口过滤)。
        baseline: 期初日期。
        end: 期末日期。

    Returns:
        区间累计收益率(小数);分母 ≤ 0(加权本金非正)或 BMV ≤ 0 时返回 None。
    """
    if bmv <= 0:
        return None
    total_days = (end - baseline).days
    if total_days <= 0:
        return None
    f_net = 0.0
    weighted = 0.0
    for d, f in flows:
        if d <= baseline or d > end:
            continue
        t_i = (d - baseline).days
        w_i = (total_days - t_i) / total_days
        f_net += f
        weighted += w_i * f
    denom = bmv + weighted
    if denom <= 0:
        return None
    return (emv - bmv - f_net) / denom


def _xirr(cashflows: List[Tuple[date, float]]) -> Optional[float]:
    """年化 XIRR(投资者视角现金流解 NPV=0),二分法。

    现金流约定(投资者视角):期初总净值 = 期初投入(−)、入金(−)、出金(+)、期末总净值(+)。
    短窗会把区间收益放大成数百 % 年化,调用方须标注「仅参考」。

    Args:
        cashflows: ``[(日期, 现金流CNY), ...]``,须含至少一正一负,按日期升序。

    Returns:
        年化内部收益率(小数);无正负号变化或求解退化时返回 None。
    """
    if len(cashflows) < 2:
        return None
    has_pos = any(cf > 0 for _, cf in cashflows)
    has_neg = any(cf < 0 for _, cf in cashflows)
    if not (has_pos and has_neg):
        return None
    t0 = cashflows[0][0]

    def _xnpv(r: float) -> float:
        return sum(
            cf / (1.0 + r) ** ((d - t0).days / 365.0) for d, cf in cashflows
        )

    lo, hi = -0.9999, 1000.0
    if _xnpv(lo) * _xnpv(hi) > 0:
        return None  # 区间内无根,放弃(避免给假值)
    mid = lo
    for _ in range(200):
        mid = (lo + hi) / 2.0
        v = _xnpv(mid)
        if abs(v) < 1e-7:
            break
        if v > 0:
            lo = mid
        else:
            hi = mid
    return mid


def compute_account_mwr(
    snapshots: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """计算各时间窗的账户口径 MWR 与可用性。

    Args:
        snapshots: 组合快照列表(每条含 date/total_market_value/exchange_rates/
            has_pricing_error 等)。顺序不限,内部按 date 升序处理。
        transactions: 交易流水行列表(``transaction_logs.jsonl`` 解析结果)。
        as_of: 计算基准「今天」;默认取系统当天。

    Returns:
        Dict[str, Any]: MWR 结构:

            - ``basis``: 固定 ``"account"``(整体账户口径)
            - ``as_of_date`` / ``generated_at``
            - ``first_snapshot_date`` / ``flow_genesis_date``(外部流可信起点)
            - ``windows``: ``{窗口名: {mwr_available, mwr_pct, mwr_xirr_annual,
              baseline_date, days, reason}}``
    """
    if as_of is None:
        as_of = date.today()

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
        windows = {
            name: {
                "mwr_available": False,
                "mwr_pct": None,
                "mwr_xirr_annual": None,
                "baseline_date": None,
                "days": 0,
                "reason": "快照不足两天,待积累",
            }
            for name in WINDOW_NAMES
        }
        return {
            "basis": "account",
            "as_of_date": as_of.strftime("%Y-%m-%d"),
            "generated_at": generated_at,
            "first_snapshot_date": clean[0][0].strftime("%Y-%m-%d") if clean else None,
            "flow_genesis_date": None,
            "windows": windows,
        }

    dates = [d for d, _ in clean]
    value_by_date: Dict[date, float] = {d: _account_value(s) for d, s in clean}
    fx_by_date: Dict[date, Dict[str, float]] = {
        d: (s.get("exchange_rates") or {}) for d, s in clean
    }
    latest_fx = fx_by_date[dates[-1]]
    first_snap, latest_snap = dates[0], dates[-1]

    flows, last_unstructured = _extract_external_flows(transactions, fx_by_date, latest_fx)
    genesis = (last_unstructured + timedelta(days=1)) if last_unstructured else first_snap

    windows: Dict[str, Any] = {}
    for name in WINDOW_NAMES:
        windows[name] = _eval_window(
            name, as_of, first_snap, latest_snap, dates,
            value_by_date, flows, last_unstructured,
        )

    return {
        "basis": "account",
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
    flows: List[Tuple[date, float]],
    last_unstructured: Optional[date],
) -> Dict[str, Any]:
    """评估单个窗口的 MWR 可用性与数值。见 ``compute_account_mwr`` 返回结构。"""
    start = _window_start(name, as_of, first_snap)
    out: Dict[str, Any] = {
        "mwr_available": False,
        "mwr_pct": None,
        "mwr_xirr_annual": None,
        "baseline_date": None,
        "days": 0,
        "reason": None,
    }
    if start is None:
        out["reason"] = "未知窗口"
        return out

    # 基准 = 最后一条 date ≤ start 的快照(窗口起点的真实净值锚点)。
    baseline: Optional[date] = None
    for d in dates:
        if d <= start:
            baseline = d
    if baseline is None:
        if name == "YTD":
            out["reason"] = f"需 {start.year} 年初基准,数据从 {first_snap} 起"
        elif name == "1Y":
            out["reason"] = "需满 12 个月数据"
        else:
            out["reason"] = f"窗口起点早于最早快照 {first_snap}"
        return out

    out["baseline_date"] = baseline.strftime("%Y-%m-%d")
    out["days"] = (latest_snap - baseline).days
    if (latest_snap - baseline).days <= 0:
        out["reason"] = "区间内快照不足两天"
        return out

    # MWR 可信门控:基准日须 ≥ 最后一笔未结构化外部流日,否则区间含无法折算的入金/出金,
    # 强算会双错(红线)。
    if last_unstructured is not None and baseline < last_unstructured:
        out["reason"] = f"资金加权待早期出入金结构化(基准 {baseline} 早于可信起点)"
        return out

    bmv = value_by_date[baseline]
    emv = value_by_date[latest_snap]
    r = _modified_dietz(bmv, emv, flows, baseline, latest_snap)
    if r is None:
        out["reason"] = "期初净值非正或加权本金退化,无法计算"
        return out
    # 一致性哨兵:区间累计 MWR 绝对值超合理上限,判定数据不自洽(外部流漏记/口径突变)。
    if abs(r) > _MAX_SANE_MWR:
        out["reason"] = (
            f"区间收益 {r * 100:.0f}% 超出合理范围,疑外部流漏记或口径突变,不可信"
        )
        return out

    # 年化 XIRR(副指标):投资者视角现金流。
    cfs: List[Tuple[date, float]] = [(baseline, -bmv)]
    for d, f in flows:
        if d <= baseline or d > latest_snap:
            continue
        # 组合视角 f(入金+/出金−)→ 投资者视角取反(入金是掏钱−,出金是取回+)。
        cfs.append((d, -f))
    cfs.append((latest_snap, emv))
    xirr = _xirr(cfs)

    out["mwr_available"] = True
    out["mwr_pct"] = round(r * 100.0, 2)
    out["mwr_xirr_annual"] = round(xirr * 100.0, 1) if xirr is not None else None
    return out

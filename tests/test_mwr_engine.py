"""account 口径 MWR 引擎单元测试——Modified Dietz / XIRR 数学正确性 + 可信门控。

纯标准库,可本地 venv 跑:
    .venv/bin/python -m pytest tests/test_mwr_engine.py -v
"""
from __future__ import annotations

from datetime import date

from stock_bot import mwr_engine as M


def _ts(y: int, mo: int, d: int) -> float:
    """把日期转成当日 12:00 的时间戳(避开时区把日期推到前一天)。"""
    from datetime import datetime
    return datetime(y, mo, d, 12, 0, 0).timestamp()


# ---------- Modified Dietz 数学 ----------

def test_modified_dietz_no_flows():
    """无中间现金流:R = (EMV−BMV)/BMV。"""
    r = M._modified_dietz(100.0, 110.0, [], date(2026, 1, 1), date(2026, 1, 11))
    assert r is not None
    assert abs(r - 0.10) < 1e-9


def test_modified_dietz_with_midflow():
    """中间入金 +50(day5,权重 0.5):分子 165−100−50=15,分母 100+0.5·50=125 → 12%。"""
    flows = [(date(2026, 1, 6), 50.0)]  # baseline=1/1, end=1/11, t_i=5, T=10
    r = M._modified_dietz(100.0, 165.0, flows, date(2026, 1, 1), date(2026, 1, 11))
    assert r is not None
    assert abs(r - 0.12) < 1e-9


def test_modified_dietz_outflow_sign():
    """出金(组合视角 −):分子含 −F_net 抵消,不把提现算成亏损。"""
    flows = [(date(2026, 1, 6), -30.0)]  # 出金 30
    # EMV=80 (100 涨到 110 后提走 30)。分子=80−100−(−30)=10;分母=100+0.5·(−30)=85
    r = M._modified_dietz(100.0, 80.0, flows, date(2026, 1, 1), date(2026, 1, 11))
    assert r is not None
    assert abs(r - (10.0 / 85.0)) < 1e-9


def test_modified_dietz_nonpositive_bmv():
    """期初净值 ≤ 0 → None(无法定义收益率)。"""
    assert M._modified_dietz(0.0, 10.0, [], date(2026, 1, 1), date(2026, 1, 11)) is None


# ---------- XIRR ----------

def test_xirr_one_year_simple():
    """−100 → +110 相隔一年,年化 ≈ 10%。"""
    r = M._xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 110.0)])
    assert r is not None
    assert abs(r - 0.10) < 1e-3


def test_xirr_needs_sign_change():
    """全同号现金流无解 → None。"""
    assert M._xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), -10.0)]) is None


def test_xirr_short_window_amplifies():
    """短窗年化放大:19 天赚 ~7% 年化应远超 100%(印证前端须标注)。"""
    r = M._xirr([(date(2026, 6, 26), -100.0), (date(2026, 7, 15), 107.0)])
    assert r is not None
    assert r > 1.0  # >100% 年化


# ---------- 外部流提取 ----------

def test_extract_only_deposits_withdrawals():
    """只认入金/出金;买卖被忽略;未结构化入金标记 last_unstructured。"""
    fx = {date(2026, 7, 1): {"USD_CNY": 7.0}}
    txns = [
        {"timestamp": _ts(2026, 7, 1), "action": "入金", "total_amount": 100.0, "currency": "USD"},
        {"timestamp": _ts(2026, 7, 1), "action": "买入", "total_amount": 50.0, "currency": "USD"},
        {"timestamp": _ts(2026, 7, 2), "action": "出金", "total_amount": 20.0, "currency": "USD"},
        {"timestamp": _ts(2026, 7, 3), "action": "入金", "details": "自由文本没金额"},
    ]
    flows, last_unstruct = M._extract_external_flows(txns, fx, {"USD_CNY": 7.0})
    # 入金 +100·7=+700,出金 −20·7=−140;买入不计
    assert flows[0] == (date(2026, 7, 1), 700.0)
    assert flows[1] == (date(2026, 7, 2), -140.0)
    assert len(flows) == 2
    assert last_unstruct == date(2026, 7, 3)


# ---------- 窗口门控 ----------

def _snap(dstr: str, tmv: float, **extra):
    s = {"date": dstr, "total_market_value": tmv, "exchange_rates": {"USD_CNY": 7.0}}
    s.update(extra)
    return s


def test_unstructured_flow_gates_window():
    """区间含未结构化外部流(基准早于可信起点)→ 该窗 mwr 不可用。"""
    snaps = [_snap("2026-07-01", 1000.0), _snap("2026-07-15", 1100.0)]
    txns = [{"timestamp": _ts(2026, 7, 5), "action": "入金", "details": "无金额自由文本"}]
    res = M.compute_account_mwr(snaps, txns, as_of=date(2026, 7, 15))
    cum = res["windows"]["累计"]
    assert cum["mwr_available"] is False
    assert "结构化" in cum["reason"]


def test_pricing_error_snapshot_excluded():
    """取价异常快照被剔除,剩不足两条 → 全窗待积累。"""
    snaps = [
        _snap("2026-07-01", 1000.0),
        _snap("2026-07-02", 999.0, has_pricing_error=True),
    ]
    res = M.compute_account_mwr(snaps, [], as_of=date(2026, 7, 2))
    assert res["windows"]["累计"]["mwr_available"] is False


def test_clean_cumulative_window_available():
    """全结构化、无异常:累计窗可用,MWR 数值正确。"""
    snaps = [_snap("2026-07-01", 1000.0), _snap("2026-07-11", 1100.0)]
    # 入金 +50 USD(·7=350)在 7/6,组合视角 +350
    txns = [{"timestamp": _ts(2026, 7, 6), "action": "入金", "total_amount": 50.0, "currency": "USD"}]
    res = M.compute_account_mwr(snaps, txns, as_of=date(2026, 7, 11))
    cum = res["windows"]["累计"]
    assert cum["mwr_available"] is True
    # 分子 1100−1000−350=−250;分母 1000+0.5·350=1175;R=−250/1175
    assert abs(cum["mwr_pct"] - round(-250.0 / 1175.0 * 100, 2)) < 0.01


def test_sanity_sentinel_blocks_absurd():
    """区间收益超 ±60% 合理上限 → 判不自洽,不给误导值。"""
    snaps = [_snap("2026-07-01", 1000.0), _snap("2026-07-11", 3000.0)]  # 净值凭空 3 倍
    res = M.compute_account_mwr(snaps, [], as_of=date(2026, 7, 11))
    cum = res["windows"]["累计"]
    assert cum["mwr_available"] is False
    assert "合理范围" in cum["reason"]


def test_insufficient_snapshots():
    """快照不足两天,结构完整返回、全窗灰置。"""
    res = M.compute_account_mwr([_snap("2026-07-01", 1000.0)], [], as_of=date(2026, 7, 1))
    assert res["basis"] == "account"
    assert all(not w["mwr_available"] for w in res["windows"].values())

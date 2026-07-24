"""validate_profile_entry 写入校验 + make_memory_tools 接线测试。

背景：2026-07-16 事故——LLM 重写持仓时写入 `成本 $60,439.27`，
$ 使成本解析失败、千分位使数值截断，持仓被静默漏算数日。
本测试锁定校验器与解析器口径一致，并验证 update_user_memory 拒绝坏格式。
"""
import json
from pathlib import Path

import pytest

from stock_bot.valuation_engine import (
    parse_cash_assets,
    parse_user_profile_to_positions,
    validate_profile_entry,
)
from core.tools.memory_tools import make_memory_tools


# ── 合法条目：现网 user_profile.json 的真实形态必须全部通过 ──────────
@pytest.mark.parametrize("key,value", [
    ("NVDA", "英伟达，4.1333 股，成本 197.20"),
    ("BTC", "比特币，0.009 枚，成本 60439.27"),
    ("3033.HK", "南方恒生科技，1200股，成本4.33"),
    ("600519", "贵州茅台，10 股，成本 1500"),
    ("BRK-B", "伯克希尔B，1 股，成本 420"),
    ("现金·汇丰", "10000 港币"),
    ("现金·Neverless", "105.59 美元"),
    ("现金·众安", "0.00 港币"),
    ("风险偏好", "中间型"),
    ("投资纪律", "购房资金不进入权益市场；不用融资融券投资股票"),
    ("历史教训", "曾把总成本当单价导致盈亏率错误"),
])
def test_valid_entries_pass(key, value):
    assert validate_profile_entry(key, value) is None


# ── 非法条目：每条都对应历史上真实发生过的坏写入 ────────────────────
@pytest.mark.parametrize("key,value,hint", [
    # 2026-07-16 事故：$ 符号使成本解析失败
    ("BTC", "比特币，0.009197 枚，成本 $60439.27", "$"),
    ("现金·Neverless", "$201.92", "$"),
    # 千分位：成本 60,439.27 会被截断成 60
    ("BTC", "比特币，0.009197 枚，成本 60,439.27", "千分位"),
    # 2026-06-24 前的非法 key：中文后缀导致持仓静默漏算
    ("BTC持仓", "比特币/BTC，0.003239枚，成本205.38", "key 非法"),
    ("持仓信息", "领航标普500ETF，1.5082股，成本679.55", "key 非法"),
    # 占位僵尸条目：应删除而非改值
    ("0700.HK", "已清空", "占位"),
    ("总资金量", "已清空，等待用户重新设置", "占位"),
    # 现金写成持仓格式 / 缺币种 / 带尾注
    ("现金·汇丰", "10000股，成本1", "持仓格式"),
    ("现金·众安", "4700", "格式错误"),
    ("现金·Neverless", "100 美元，策略账户余额", "格式错误"),
    # ticker key 配非持仓 value：解析器会静默跳过
    ("NVDA", "观察中，等待回调", "持仓类 key"),
    # 份额为 0：应删除
    ("QQQ", "纳指100ETF，0 股，成本 260.14", "大于 0"),
    # 缺成本关键词
    ("TSM", "台积电，1.4079 股，单价 434.82", "成本"),
])
def test_invalid_entries_rejected(key, value, hint):
    err = validate_profile_entry(key, value)
    assert err is not None and err.startswith("❌"), f"{key}={value} 应被拒绝"
    assert hint in err, f"错误消息应含 '{hint}'：{err}"


# ── 口径一致性：校验通过的持仓/现金必须能被解析器解析出来 ───────────
def test_validator_parser_alignment():
    profile = {
        "NVDA": "英伟达，4.1333 股，成本 197.20",
        "BTC": "比特币，0.009 枚，成本 60439.27",
        "现金·汇丰": "10000 港币",
        "现金·Neverless": "105.59 美元",
    }
    for k, v in profile.items():
        assert validate_profile_entry(k, v) is None
    positions = parse_user_profile_to_positions(profile)
    assert set(positions) == {"NVDA", "BTC"}
    assert positions["BTC"]["shares"] == 0.009
    assert positions["BTC"]["cost_basis"] == 60439.27
    cash = parse_cash_assets(profile)
    assert {c["currency"] for c in cash} == {"HKD", "USD"}


# ── 工具层接线：update_user_memory 被拒绝时不落盘 ───────────────────
def test_update_tool_rejects_and_skips_write(tmp_path: Path):
    tools = make_memory_tools(tmp_path, validate_entry=validate_profile_entry)
    update = next(t for t in tools if t.name == "update_user_memory")

    # 坏写入：拒绝且不产生文件内容
    result = update.invoke({"key": "BTC", "value": "比特币，0.009 枚，成本 $60,439.27"})
    assert result.startswith("❌")
    profile_path = tmp_path / "user_profile.json"
    data = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
    assert "BTC" not in data

    # 好写入：正常落盘
    result = update.invoke({"key": "BTC", "value": "比特币，0.009 枚，成本 60439.27"})
    assert result.startswith("✅")
    data = json.loads(profile_path.read_text(encoding="utf-8"))
    assert data["BTC"] == "比特币，0.009 枚，成本 60439.27"


def test_no_validator_keeps_legacy_behavior(tmp_path: Path):
    """ehs/mhxy 不传校验器：任意写入照旧成功。"""
    tools = make_memory_tools(tmp_path)
    update = next(t for t in tools if t.name == "update_user_memory")
    result = update.invoke({"key": "企业信息", "value": "$100 预算，1,000 人工厂"})
    assert result.startswith("✅")

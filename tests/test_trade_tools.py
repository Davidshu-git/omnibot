"""record_trade 原子交易工具 + 幻觉写入校验正则的单元测试。"""
import json
from pathlib import Path

import pytest

from stock_bot.tools.trade_tools import make_trade_tools
from stock_bot.valuation_engine import parse_user_profile_to_positions, parse_cash_assets
from core.tg_base import _MEMORY_WRITE_CLAIM_PATTERN


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    profile = {
        "风险偏好": "中间型",
        "NVDA": "英伟达，2.2548 股，成本 199.10",
        "BTC": "比特币，0.009197 枚，成本 60438.90",
        "现金·Neverless": "946.11 美元",
        "现金·汇丰": "10000 港币",
    }
    (tmp_path / "user_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def _tool(memory_dir: Path):
    return make_trade_tools(memory_dir)[0]


def _profile(memory_dir: Path) -> dict:
    return json.loads((memory_dir / "user_profile.json").read_text(encoding="utf-8"))


def _log_lines(memory_dir: Path) -> list[dict]:
    p = memory_dir / "transaction_logs.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


class TestBuy:
    def test_buy_existing_position_recomputes_avg_cost_and_deducts_cash(self, memory_dir):
        # 复现 2026-07-07 事故场景：146.16 美元买 0.7485 股 NVDA，Neverless 扣款
        out = _tool(memory_dir).invoke({
            "action": "买入", "total_amount": 146.16,
            "ticker": "NVDA", "shares": 0.7485, "cash_account": "Neverless",
        })
        assert out.startswith("✅")
        data = _profile(memory_dir)
        assert data["NVDA"] == "英伟达，3.0033 股，成本 198.15"
        assert data["现金·Neverless"] == "799.95 美元"
        # 回执必须回显落库值
        assert "3.0033 股" in out and "799.95 美元" in out
        # 流水已追加
        logs = _log_lines(memory_dir)
        assert len(logs) == 1 and logs[0]["action"] == "买入" and logs[0]["target"] == "NVDA"
        # 引擎解析器能读回
        pos = parse_user_profile_to_positions(data)
        assert pos["NVDA"]["shares"] == 3.0033 and pos["NVDA"]["cost_basis"] == 198.15

    def test_buy_new_position_requires_name(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "买入", "total_amount": 100.0, "ticker": "AAPL", "shares": 0.5,
        })
        assert out.startswith("❌") and "name" in out
        assert "AAPL" not in _profile(memory_dir)

    def test_buy_new_crypto_uses_mei_unit(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "买入", "total_amount": 100.0,
            "ticker": "ETH", "shares": 0.04, "name": "以太坊",
        })
        assert out.startswith("✅")
        data = _profile(memory_dir)
        assert data["ETH"] == "以太坊，0.04 枚，成本 2500.00"
        assert parse_user_profile_to_positions(data)["ETH"]["type"] == "crypto"

    def test_buy_insufficient_cash_rejected_atomically(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "买入", "total_amount": 99999.0,
            "ticker": "NVDA", "shares": 500.0, "cash_account": "Neverless",
        })
        assert out.startswith("❌") and "余额不足" in out
        data = _profile(memory_dir)
        # 持仓与现金都不应被改动
        assert data["NVDA"] == "英伟达，2.2548 股，成本 199.10"
        assert data["现金·Neverless"] == "946.11 美元"
        assert _log_lines(memory_dir) == []


class TestSell:
    def test_sell_partial_keeps_cost_credits_cash(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "卖出", "total_amount": 195.55,
            "ticker": "NVDA", "shares": 1.0, "cash_account": "Neverless",
        })
        assert out.startswith("✅")
        data = _profile(memory_dir)
        assert data["NVDA"] == "英伟达，1.2548 股，成本 199.10"
        assert data["现金·Neverless"] == "1141.66 美元"

    def test_sell_all_deletes_position(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "卖出", "total_amount": 555.0, "ticker": "BTC", "shares": 0.009197,
        })
        assert out.startswith("✅") and "清仓" in out
        assert "BTC" not in _profile(memory_dir)

    def test_oversell_rejected(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "卖出", "total_amount": 1000.0, "ticker": "NVDA", "shares": 5.0,
        })
        assert out.startswith("❌") and "不足以卖出" in out

    def test_sell_unknown_ticker_rejected(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "卖出", "total_amount": 10.0, "ticker": "TSLA", "shares": 1.0,
        })
        assert out.startswith("❌")


class TestCashFlow:
    def test_deposit_existing_account(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "入金", "total_amount": 146.10, "cash_account": "Neverless",
        })
        assert out.startswith("✅")
        assert _profile(memory_dir)["现金·Neverless"] == "1092.21 美元"
        # 引擎现金解析器能读回且币种正确
        cash = parse_cash_assets(_profile(memory_dir))
        nl = next(c for c in cash if c["platform"] == "Neverless")
        assert nl["amount"] == 1092.21 and nl["currency"] == "USD"

    def test_withdraw_insufficient_rejected(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "出金", "total_amount": 99999.0, "cash_account": "汇丰",
        })
        assert out.startswith("❌") and "余额不足" in out
        assert _profile(memory_dir)["现金·汇丰"] == "10000 港币"

    def test_withdraw_hkd_keeps_currency_word(self, memory_dir):
        out = _tool(memory_dir).invoke({
            "action": "出金", "total_amount": 1000.0, "cash_account": "汇丰",
        })
        assert out.startswith("✅")
        assert _profile(memory_dir)["现金·汇丰"] == "9000.00 港币"


class TestValidation:
    def test_invalid_action(self, memory_dir):
        out = _tool(memory_dir).invoke({"action": "梭哈", "total_amount": 1.0})
        assert out.startswith("❌")

    def test_trade_missing_ticker(self, memory_dir):
        out = _tool(memory_dir).invoke({"action": "买入", "total_amount": 1.0})
        assert out.startswith("❌")

    def test_deposit_missing_account(self, memory_dir):
        out = _tool(memory_dir).invoke({"action": "入金", "total_amount": 1.0})
        assert out.startswith("❌")


class TestWriteClaimPattern:
    """幻觉写入校验正则：必须命中事故中的真实话术，且不误伤合法表述。"""

    @pytest.mark.parametrize("text", [
        "交易流水已录，记忆已同步。",           # 2026-07-07 事故原话
        "记忆已更新 ✅",
        "已更新 NVDA 持仓记忆",
        "持仓档案已写入最新数据",
        "现金余额已同步",
        "我已把这笔交易写入记忆库",
        "流水已追加记录",                        # "流水...已...录" 变体
    ])
    def test_positive(self, text):
        assert _MEMORY_WRITE_CLAIM_PATTERN.search(text), text

    @pytest.mark.parametrize("text", [
        "预警已更新为 200 美元",                 # 预警不是记忆写入
        "您的持仓如下：NVDA 3 股",               # 纯陈述
        "K 线图已生成",
        "报告已保存到工作区",
        "记忆里目前有 5 条持仓记录",             # 读取类陈述
    ])
    def test_negative(self, text):
        assert not _MEMORY_WRITE_CLAIM_PATTERN.search(text), text

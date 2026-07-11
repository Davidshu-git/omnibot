"""snapshot._aggregate_realized_pnl 的单元测试：流水已实现盈亏 → CNY 折算。"""
import json
from pathlib import Path

import pytest

from stock_bot import snapshot


@pytest.fixture
def log_file(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "transaction_logs.jsonl"
    monkeypatch.setattr(snapshot, "TRANSACTION_LOG_FILE", p)
    return p


def _write(p: Path, rows: list) -> None:
    p.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


RATES = {"HKD_CNY": 0.87, "USD_CNY": 7.20}


class TestAggregateRealizedPnl:
    def test_sums_per_currency_and_folds_at_given_rates(self, log_file):
        _write(log_file, [
            {"action": "卖出", "realized_pnl": 348.0, "currency": "HKD"},
            {"action": "卖出", "realized_pnl": -7.25, "currency": "USD"},
            {"action": "卖出", "realized_pnl": 4.44, "currency": "USD"},
        ])
        by_cur, total = snapshot._aggregate_realized_pnl(RATES)
        assert by_cur == {"HKD": 348.0, "USD": -2.81}
        # 348×0.87 + (−2.81)×7.2 = 302.76 − 20.23 = 282.53
        assert total == round(348.0 * 0.87 + (-7.25 + 4.44) * 7.20, 2)

    def test_legacy_freetext_rows_skipped(self, log_file):
        _write(log_file, [
            {"action": "卖出", "target": "QQQ", "details": "自由文本旧流水，无结构化字段"},
            {"action": "买入", "ticker": "NVDA", "currency": "USD"},  # 买入无 realized_pnl
            {"action": "卖出", "realized_pnl": 10.0, "currency": "USD"},
        ])
        by_cur, total = snapshot._aggregate_realized_pnl(RATES)
        assert by_cur == {"USD": 10.0} and total == 72.0

    def test_missing_file_returns_zero(self, log_file):
        by_cur, total = snapshot._aggregate_realized_pnl(RATES)
        assert by_cur == {} and total == 0.0

    def test_bad_lines_and_bool_pnl_ignored(self, log_file):
        log_file.write_text(
            "不是json\n" + json.dumps({"realized_pnl": True, "currency": "USD"}) + "\n"
            + json.dumps({"realized_pnl": 5.0, "currency": "HKD"}) + "\n",
            encoding="utf-8",
        )
        by_cur, total = snapshot._aggregate_realized_pnl(RATES)
        assert by_cur == {"HKD": 5.0} and total == 4.35

    def test_unknown_currency_falls_back_to_cny_rate_1(self, log_file):
        _write(log_file, [{"realized_pnl": 3.0, "currency": "CNY"}])
        by_cur, total = snapshot._aggregate_realized_pnl(RATES)
        assert by_cur == {"CNY": 3.0} and total == 3.0

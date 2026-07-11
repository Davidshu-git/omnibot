"""
原子化交易落库工具 - 一次调用完成：持仓均价重算 + 现金增减 + 交易流水追加。

背景：此前买入/卖出依赖 LLM 连续正确调用 update_user_memory（持仓）+
update_user_memory（现金）+ append_transaction_log（流水）并自行心算均价，
幻觉面过大——曾发生模型一个工具都没调却回复"已更新 ✅"的事故（2026-07-07）。
本模块把整笔交易收敛为单个工具调用，内部保证格式与 valuation_engine 解析器严格兼容，
并在返回值中回显落库后的真实档案值，供模型直接引用。
"""
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from filelock import FileLock
from langchain_core.tools import tool

from stock_bot.valuation_engine import (
    detect_ticker_currency,
    format_universal_ticker,
    is_crypto_ticker,
)

import logging

logger = logging.getLogger(__name__)

# 与 valuation_engine.parse_user_profile_to_positions 保持同一口径：
# 份额单位 股/枚/个，成本关键词固定为"成本"
_SHARES_PATTERN = re.compile(r'([\d.]+)\s*(股|枚|个)')
_COST_PATTERN = re.compile(r'成本\s*([\d.]+)')
_CASH_AMOUNT_PATTERN = re.compile(r'([\d,]+\.?\d*)')
_CASH_CURRENCY_WORDS = ("美元", "美金", "港币", "港元", "人民币", "元")

_EPSILON = 1e-9
# 剩余份额低于此阈值视为清仓（浮点误差容忍）
_CLEARED_THRESHOLD = 1e-6


def _fmt_shares(x: float) -> str:
    """份额格式化：最多 6 位小数，去掉尾随零。"""
    return f"{x:.6f}".rstrip("0").rstrip(".")


def _fmt_cost(x: float) -> str:
    """单位成本格式化：≥1 保留 2 位小数；<1（低价 ETF/汇率类）保留 4 位。"""
    return f"{x:.2f}" if x >= 1 else f"{x:.4f}".rstrip("0").rstrip(".")


def _parse_position(value: str) -> Optional[Tuple[str, float, str, float]]:
    """解析持仓 value 字符串。

    Args:
        value: 形如 '英伟达，2.2548 股，成本 199.10' 的持仓描述。

    Returns:
        (中文名称, 份额, 份额单位, 单位成本)；解析失败返回 None。
    """
    shares_match = _SHARES_PATTERN.search(value)
    cost_match = _COST_PATTERN.search(value)
    if not shares_match or not cost_match:
        return None
    name = "-"
    parts = value.replace('，', ',').split(',')
    if parts:
        first = parts[0].split(' ')[0].strip()
        if first and not re.match(r'^\d', first):
            name = first
    return name, float(shares_match.group(1)), shares_match.group(2), float(cost_match.group(1))


def _parse_cash(value: str) -> Optional[Tuple[float, str]]:
    """解析现金 value 字符串（如 '946.11 美元'）→ (金额, 币种词)。"""
    vs = str(value).replace("，", ",")
    m = _CASH_AMOUNT_PATTERN.search(vs)
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    currency_word = next((w for w in _CASH_CURRENCY_WORDS if w in vs), "元")
    return amount, currency_word


def _find_cash_key(data: Dict[str, Any], platform: str) -> Optional[str]:
    """按平台名定位现金条目 key：先精确匹配 `现金·平台`，再模糊包含。"""
    exact = f"现金·{platform}"
    if exact in data:
        return exact
    for k in data:
        if str(k).startswith("现金") and platform in str(k):
            return k
    return None


def make_trade_tools(memory_dir: Path) -> list:
    """创建绑定记忆目录的原子交易工具列表。

    Args:
        memory_dir: 记忆文件目录（存放 user_profile.json、transaction_logs.jsonl）。

    Returns:
        list: 含 record_trade 的 LangChain 工具列表。
    """
    memory_dir.mkdir(parents=True, exist_ok=True)
    profile_path = memory_dir / "user_profile.json"
    lock_path = memory_dir / "user_profile.json.lock"
    log_path = memory_dir / "transaction_logs.jsonl"

    @tool
    def record_trade(
        action: str,
        total_amount: float,
        ticker: str = "",
        shares: float = 0.0,
        name: str = "",
        cash_account: str = "",
    ) -> str:
        """
        🚨【交易原子落库指令】（买卖/出入金的唯一入口）：
        用户告知一笔【买入 / 卖出 / 入金 / 出金】时，必须且只能调用本工具一次，
        它会原子化完成：持仓均价重算 + 现金余额增减 + 交易流水追加。
        **绝对禁止**再用 update_user_memory + append_transaction_log 手工组合记录交易！

        - 参数 action: 只能是 买入 / 卖出 / 入金 / 出金 四者之一。
        - 参数 total_amount: 本笔交易的**总金额**（标的原生币种；买入=总花费，卖出=总回款，
          出入金=变动金额）。注意用户说"146.16美元购入0.7485股"时 146.16 是总花费不是单价。
        - 参数 ticker: 标的裸代码/币种符号（如 NVDA、600519、3033.HK、BTC）。买入/卖出必填。
        - 参数 shares: 本笔交易的份额数（支持小数碎股）。买入/卖出必填。
        - 参数 name: 标的中文名称（如 "英伟达"）。首次买入新标的时必填；已有持仓可省略。
        - 参数 cash_account: 资金账户平台名（如 "Neverless"、"汇丰"）。入金/出金必填；
          买入/卖出时若用户指明了扣款/回款账户则填写（会同步增减该账户现金），
          未指明则留空。币种必须与交易币种一致，工具不做汇率换算。

        返回落库后的真实档案值——回复用户时必须原样引用这些数字，禁止自行计算。
        """
        action = action.strip()
        if action not in ("买入", "卖出", "入金", "出金"):
            return f"❌ 参数错误：action 必须是 买入/卖出/入金/出金，收到 '{action}'"
        if total_amount <= 0:
            return f"❌ 参数错误：total_amount 必须为正数，收到 {total_amount}"

        is_trade = action in ("买入", "卖出")
        if is_trade:
            ticker = ticker.strip()
            if not ticker or shares <= 0:
                return "❌ 参数错误：买入/卖出必须提供 ticker 和正数 shares"
        elif not cash_account.strip():
            return "❌ 参数错误：入金/出金必须提供 cash_account"

        try:
            with FileLock(lock_path, timeout=5):
                data: Dict[str, Any] = {}
                if profile_path.exists():
                    with open(profile_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)

                receipt_lines: list[str] = []
                warnings: list[str] = []

                # ---- 第一步：全部校验与计算（不写盘）----
                new_position_value: Optional[str] = None
                position_cleared = False
                # 卖出时的已实现盈亏（原生币种）。落进流水的结构化字段，供
                # snapshot 汇总进总控台「累计收益」——清仓后持仓条目被删除，
                # 浮盈若不在此刻转为已实现记录就会凭空蒸发（3033.HK 事故）。
                realized_pnl: Optional[float] = None
                trade_currency = (
                    detect_ticker_currency(format_universal_ticker(ticker))
                    if is_trade else ""
                )
                if is_trade:
                    old = _parse_position(str(data[ticker])) if ticker in data else None
                    if ticker in data and old is None:
                        return (
                            f"❌ 持仓条目 [{ticker}] 现有格式无法解析：'{data[ticker]}'。"
                            f"请先用 update_user_memory 修复为标准格式后重试。"
                        )
                    if action == "买入":
                        if old is None:
                            if not name.strip():
                                return (
                                    f"❌ [{ticker}] 是新持仓，必须提供 name（中文名称）参数。"
                                )
                            unit = "枚" if is_crypto_ticker(ticker) else "股"
                            new_shares, new_cost = shares, total_amount / shares
                            pos_name = name.strip()
                        else:
                            pos_name, old_shares, unit, old_cost = old
                            if name.strip():
                                pos_name = name.strip()
                            new_shares = old_shares + shares
                            new_cost = (old_shares * old_cost + total_amount) / new_shares
                        new_position_value = (
                            f"{pos_name}，{_fmt_shares(new_shares)} {unit}，成本 {_fmt_cost(new_cost)}"
                        )
                    else:  # 卖出
                        if old is None:
                            return f"❌ 卖出失败：档案中没有 [{ticker}] 的持仓记录。"
                        pos_name, old_shares, unit, old_cost = old
                        if shares > old_shares + _EPSILON:
                            return (
                                f"❌ 卖出失败：[{ticker}] 当前仅持有 {_fmt_shares(old_shares)} {unit}，"
                                f"不足以卖出 {_fmt_shares(shares)} {unit}。"
                            )
                        realized_pnl = total_amount - old_cost * shares
                        remaining = old_shares - shares
                        if remaining < _CLEARED_THRESHOLD:
                            position_cleared = True
                        else:
                            new_position_value = (
                                f"{pos_name}，{_fmt_shares(remaining)} {unit}，成本 {_fmt_cost(old_cost)}"
                            )

                new_cash_value: Optional[str] = None
                cash_key: Optional[str] = None
                if cash_account.strip():
                    cash_key = _find_cash_key(data, cash_account.strip())
                    cash_delta = (
                        total_amount if action in ("卖出", "入金") else -total_amount
                    )
                    if cash_key is None:
                        if action in ("入金", "卖出"):
                            cash_key = f"现金·{cash_account.strip()}"
                            new_cash_value = f"{cash_delta:.2f} 美元"
                            warnings.append(
                                f"⚠️ 现金账户 [{cash_key}] 不存在，已按默认币种『美元』新建；"
                                f"若币种不对，请立即用 update_user_memory 修正。"
                            )
                        else:
                            return (
                                f"❌ 现金账户 '{cash_account}' 在档案中不存在，无法扣款。"
                                f"请确认平台名，或先用 update_user_memory 建立该现金条目。"
                            )
                    else:
                        parsed = _parse_cash(str(data[cash_key]))
                        if parsed is None:
                            return (
                                f"❌ 现金条目 [{cash_key}] 现有格式无法解析：'{data[cash_key]}'。"
                                f"请先修复为『金额 币种』格式后重试。"
                            )
                        old_amount, currency_word = parsed
                        new_amount = old_amount + cash_delta
                        if new_amount < -_EPSILON:
                            return (
                                f"❌ 余额不足：[{cash_key}] 当前 {old_amount:.2f} {currency_word}，"
                                f"不足以支出 {total_amount:.2f}。请与用户核实金额或账户。"
                            )
                        new_cash_value = f"{max(new_amount, 0.0):.2f} {currency_word}"

                # ---- 第二步：一次性写盘 ----
                if new_position_value is not None:
                    data[ticker] = new_position_value
                    receipt_lines.append(f"持仓 [{ticker}] -> '{new_position_value}'")
                if position_cleared:
                    data.pop(ticker, None)
                    receipt_lines.append(f"持仓 [{ticker}] 已清仓，条目已删除")
                if cash_key is not None and new_cash_value is not None:
                    data[cash_key] = new_cash_value
                    receipt_lines.append(f"现金 [{cash_key}] -> '{new_cash_value}'")

                with open(profile_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

            # ---- 第三步：追加交易流水（独立文件，追加写）----
            if is_trade:
                unit_price = total_amount / shares
                details = (
                    f"{action} {_fmt_shares(shares)} {unit}，总金额 {total_amount:.2f}，"
                    f"单价约 {_fmt_cost(unit_price)}"
                    + (f"，{cash_account} 账户{'回款' if action == '卖出' else '支付'}"
                       if cash_account.strip() else "")
                    + (f"，已实现盈亏 {realized_pnl:+.2f} {trade_currency}"
                       if realized_pnl is not None else "")
                )
                log_target = ticker
            else:
                details = f"{action} {total_amount:.2f}，账户余额变更为 '{new_cash_value}'"
                log_target = cash_key or f"现金·{cash_account.strip()}"
            record: Dict[str, Any] = {
                "timestamp": time.time(),
                "action": action,
                "target": log_target,
                "details": details,
            }
            if is_trade:
                # 结构化字段（details 之外的机器可读口径）：snapshot 汇总
                # realized_pnl 折算进总控台；ticker/shares/total_amount 为
                # 后续 TWR 时间加权收益的现金流地基。金额均为标的原生币种。
                record.update({
                    "ticker": ticker,
                    "shares": shares,
                    "total_amount": round(total_amount, 2),
                    "currency": trade_currency,
                })
                if realized_pnl is not None:
                    record["realized_pnl"] = round(realized_pnl, 2)
            entry = json.dumps(record, ensure_ascii=False)
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(entry + "\n")
            receipt_lines.append(f"流水已追加：{action} {log_target}（{details}）")
            if realized_pnl is not None:
                receipt_lines.append(
                    f"本笔已实现盈亏：{realized_pnl:+.2f} {trade_currency}"
                    f"（已计入流水，总控台累计收益按最新汇率折算展示）"
                )

            receipt = "\n".join(f"- {line}" for line in receipt_lines)
            suffix = ("\n" + "\n".join(warnings)) if warnings else ""
            return (
                f"✅ 交易已原子落库。以下为写入后的真实档案值，"
                f"回复用户时必须原样引用，禁止自行计算：\n{receipt}{suffix}"
            )
        except json.JSONDecodeError:
            return "❌ 记忆文件损坏：JSONDecodeError"
        except TimeoutError:
            return "❌ 文件锁超时：其他进程正在写入记忆"
        except Exception as e:
            logger.error(f"record_trade 失败：{type(e).__name__}: {e}")
            return f"❌ 交易落库失败：{type(e).__name__} - {str(e)}"

    return [record_trade]

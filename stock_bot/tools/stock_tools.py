"""
股票领域专属工具工厂 - 供 StockBot Agent 注册使用。

通过 make_stock_tools() 工厂函数生成绑定了运行时目录和配置的工具列表。
"""
import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from filelock import FileLock
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from stock_bot.valuation_engine import (
    fetch_stock_price_raw,
    fetch_etf_price_raw,
    generate_kline_chart,
    calculate_portfolio_valuation,
    parse_user_profile_to_positions,
    format_portfolio_report,
)

logger = logging.getLogger(__name__)


def make_stock_tools(
    sandbox_dir: Path,
    memory_dir: Path,
    allowed_tg_users: str = "",
) -> list:
    """
    创建股票领域专属工具列表。

    Args:
        sandbox_dir:       Agent 工作区目录（K 线图输出路径）
        memory_dir:        记忆目录（读取 user_profile.json，写入 alerts.json）
        allowed_tg_users:  ALLOWED_TG_USERS 环境变量原始字符串，用于写入预警的 chat_id 分组
    """
    user_profile_path = memory_dir / "user_profile.json"
    alerts_file = memory_dir / "alerts.json"
    alerts_lock = memory_dir / "alerts.json.lock"

    @tool
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    )
    def get_universal_stock_price(ticker: str, date: Optional[str] = None) -> str:
        """
        🌐 全球股票查价引擎（支持美股、A 股、港股）。
        只需传入用户提到的代码即可（例如：AAPL, 600519, 0700），底层会自动判断市场。
        - 参数 date (可选): 'YYYY-MM-DD'。未提供则默认返回最近交易日。
        """
        price_data = fetch_stock_price_raw(ticker, date)
        return (
            f"✅ {price_data['ticker']} ({price_data['date']}) - "
            f"开盘价：{price_data['open']}, 收盘价：{price_data['close']}"
        )

    @tool
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    )
    def get_etf_price(etf_code: str, date: Optional[str] = None) -> str:
        """
        🇨🇳 A 股 ETF 基金专用查价引擎（支持 akshare 和 yfinance 双数据源）。
        当用户查询 ETF 基金（如 513050、159915、510300 等）时优先使用此工具。
        - 参数 etf_code: 6 位 ETF 代码（如 '513050'）
        - 参数 date (可选): 'YYYY-MM-DD'。未提供则返回最近交易日数据。
        """
        price_data = fetch_etf_price_raw(etf_code, date)
        if price_data.get("source") == "akshare_spot":
            return (
                f"✅ ETF {etf_code} 实时行情 - 最新价：{price_data['current_price']} "
                f"({price_data['change_percent']}%)\n"
                f"开盘：{price_data['open']}, 最高：{price_data['high']}, "
                f"最低：{price_data['low']}, 昨收：{price_data['prev_close']}\n"
                f"成交量：{price_data['volume']}手，成交额：{price_data['amount']}万元"
            )
        else:
            return (
                f"✅ ETF {etf_code} ({price_data['date']}) - "
                f"开盘：{price_data['open']}, 收盘：{price_data['close']}, "
                f"最高：{price_data['high']}, 最低：{price_data['low']}, "
                f"成交量：{price_data['volume']}"
            )

    @tool
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    )
    def draw_universal_stock_chart(ticker: str, days: int = 30) -> str:
        """
        🌐 全球股票走势绘图引擎（支持美股、A 股、港股，支持自定义时间跨度）。

        Args:
            ticker: 股票代码（如 AAPL, 600519, 0700）
            days:   K 线图时间跨度（天数）。默认 30 天。半年传 180，一年传 365。
        """
        chart_data = generate_kline_chart(ticker, sandbox_dir, days)
        return (
            f"✅ {chart_data['ticker']} {days}天走势图生成完毕！文件名为：{chart_data['file_name']}。\n"
            f"【摘要】最高：{chart_data['max_price']}, 最低：{chart_data['min_price']}, "
            f"最新：{chart_data['latest_close']}。\n"
            f"🚨【强制语法】：必须严格使用 `![走势图](./{chart_data['file_name']})` 嵌入 Markdown 中！"
        )

    @tool
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    )
    def search_company_ticker(company_name: str) -> str:
        """
        当你不知道某家公司、产品或品牌的具体股票代码时，必须先使用此工具。
        输入公司或产品名称（如 'aws', '淘宝', '马斯克的公司'），联网搜索并返回相关信息。
        """
        import requests
        try:
            from ddgs import DDGS
            query = f"{company_name} 股票代码 ticker symbol"
            ddgs = DDGS()
            results = ddgs.text(query, max_results=3)
            if not results:
                return f"未搜索到 {company_name} 的相关股票代码。"
            return str(results)
        except requests.exceptions.Timeout:
            return f"联网搜索超时：搜索 '{company_name}' 时超过 10 秒无响应，请稍后重试。"
        except requests.exceptions.ConnectionError:
            return f"网络连接失败：无法连接到搜索服务，请检查网络状态。"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if hasattr(e, 'response') else 'ERROR'
            return f"搜索服务返回错误：HTTP {status}。"
        except Exception as e:
            return f"联网搜索出错：{type(e).__name__} - {str(e)}"

    @tool
    def calculate_exact_portfolio_value() -> str:
        """
        💰【个人持仓市值精确计算器】（财务计算专属红线）：
        当用户询问自己的总资产、总市值、具体盈亏金额，或者要求盘点当前账户资金情况时，
        **必须且只能**调用此工具！**严禁自行心算或数学推演！**
        """
        try:
            if not user_profile_path.exists():
                return "❌ 未找到持仓记忆文件，请先告知我您的持仓情况。"
            with open(user_profile_path, 'r', encoding='utf-8') as f:
                user_data = json.load(f)
            if not user_data:
                return "❌ 持仓记忆为空，请先告知我您的持仓情况。"
            positions = parse_user_profile_to_positions(user_data)
            if not positions:
                return "❌ 未解析到有效持仓数据，请检查持仓记忆格式。"
            valuation = calculate_portfolio_valuation(positions)
            return format_portfolio_report(valuation)
        except json.JSONDecodeError:
            return "❌ 持仓记忆文件损坏：JSONDecodeError"
        except Exception as e:
            return f"❌ 计算失败：{type(e).__name__} - {str(e)}"

    @tool
    def create_price_alert(ticker: str, operator: str, target_price: float) -> str:
        """
        🚨【自然语言盯盘预警创建器】：
        当用户用自然语言要求"盯着"、"跌破"、"突破"、"涨过"某个具体价格时提醒他，必须调用此工具！

        Args:
            ticker:       股票代码（如 AAPL, 0700.HK, 513050.SS）
            operator:     必须严格输出 '<'（跌破/低于）或 '>'（突破/高于/涨过）
            target_price: 具体触发价格（纯数字）
        """
        try:
            with FileLock(alerts_lock, timeout=5):
                current_alerts: dict = {}
                if alerts_file.exists():
                    try:
                        with open(alerts_file, 'r', encoding='utf-8') as f:
                            current_alerts = json.load(f)
                    except json.JSONDecodeError:
                        logger.warning("alerts.json 文件损坏，将重建")

                users = [u.strip() for u in allowed_tg_users.split(",") if u.strip().isdigit()]
                admin_id = users[0] if users else "default"

                if admin_id not in current_alerts:
                    current_alerts[admin_id] = {}

                task_key = f"{ticker}_{operator}_{target_price}"
                current_alerts[admin_id][task_key] = {
                    "ticker": ticker,
                    "operator": operator,
                    "target_price": target_price,
                    "created_at": datetime.now().isoformat(),
                }

                alerts_file.parent.mkdir(parents=True, exist_ok=True)
                with open(alerts_file, 'w', encoding='utf-8') as f:
                    json.dump(current_alerts, f, ensure_ascii=False, indent=2)

        except TimeoutError:
            return "❌ 预警写入失败：文件锁超时，请稍后重试"
        except Exception as e:
            logger.error(f"写入预警失败：{type(e).__name__} - {e}")
            return f"❌ 预警写入失败：{type(e).__name__} - {e}"

        return (
            f"✅ 预警任务已安全挂载至底层引擎：当 {ticker} {operator} {target_price} 时将自动拦截并通知用户。"
        )

    return [
        get_universal_stock_price,
        get_etf_price,
        draw_universal_stock_chart,
        search_company_ticker,
        calculate_exact_portfolio_value,
        create_price_alert,
    ]

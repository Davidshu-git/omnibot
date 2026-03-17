"""
估值引擎核心模块 - 纯 Python 量化查价与绘图底层逻辑。

本模块提供：
1. 全球股票价格查询（支持美股/A 股/港股）
2. A 股 ETF 价格查询（双源降级）
3. K 线图生成
4. 持仓估值计算
"""

import socket
import yfinance as yf
import akshare as ak
import mplfinance as mpf
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import logging
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

socket.setdefaulttimeout(30)


class AkshareTimeoutError(Exception):
    """akshare 请求超时异常"""
    pass

DEFAULT_EXCHANGE_RATES = {
    "USD_CNY": 7.20,
    "HKD_CNY": 0.92,
    "CNY_CNY": 1.0
}


@retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(2),
    retry=retry_if_exception_type((requests.exceptions.Timeout, requests.exceptions.ConnectionError))
)
def _fetch_akshare_rate(symbol: str, today: str) -> Optional[float]:
    """
    带重试和超时保护的 akshare 汇率获取函数。
    
    Args:
        symbol: "美元" 或 "港币"
        today: 日期字符串 YYYYMMDD
    
    Returns:
        Optional[float]: 汇率值，失败返回 None
    """
    try:
        df = ak.currency_boc_sina(symbol=symbol, start_date=today, end_date=today)
        if df is not None and not df.empty:
            return float(df['现汇买入价'].iloc[-1])
    except requests.exceptions.Timeout:
        logger.warning(f"akshare 获取 {symbol} 汇率超时")
        raise
    except requests.exceptions.ConnectionError:
        logger.warning(f"akshare 获取 {symbol} 汇率连接失败")
        raise
    except Exception as e:
        logger.debug(f"akshare 获取 {symbol} 汇率异常：{type(e).__name__}")
        raise
    return None


def fetch_exchange_rates() -> Dict[str, float]:
    """
    获取实时汇率（USD/CNY, HKD/CNY），使用 akshare 为主数据源，yfinance 为备选。
    
    Returns:
        Dict[str, float]: 汇率字典，如 {"USD_CNY": 7.25, "HKD_CNY": 0.93, "CNY_CNY": 1.0}
    
    Note:
        如果所有数据源失败，返回硬编码的默认值防止引擎崩溃。
    """
    rates = DEFAULT_EXCHANGE_RATES.copy()
    today = datetime.now().strftime("%Y%m%d")
    
    # 获取 USD/CNY
    usd_rate = None
    try:
        usd_rate = _fetch_akshare_rate("美元", today)
    except Exception:
        pass
    
    if usd_rate is None:
        logger.debug("akshare 获取 USD/CNY 失败，尝试 yfinance")
        try:
            usd_cny = yf.Ticker("USDCNY=X").history(period="1d", timeout=10)
            if usd_cny is not None and not usd_cny.empty and 'Close' in usd_cny.columns:
                close_val = usd_cny['Close'].iloc[-1]
                if pd.notna(close_val):
                    usd_rate = float(close_val)
        except Exception as e:
            logger.warning(f"yfinance 获取 USD/CNY 失败，使用默认值：{type(e).__name__}")
    
    if usd_rate:
        rates["USD_CNY"] = round(usd_rate, 4)
    
    # 获取 HKD/CNY
    hkd_rate = None
    try:
        hkd_rate = _fetch_akshare_rate("港币", today)
    except Exception:
        pass
    
    if hkd_rate is None:
        logger.debug("akshare 获取 HKD/CNY 失败，尝试 yfinance")
        try:
            hkd_cny = yf.Ticker("HKDCNY=X").history(period="1d", timeout=10)
            if hkd_cny is not None and not hkd_cny.empty and 'Close' in hkd_cny.columns:
                close_val = hkd_cny['Close'].iloc[-1]
                if pd.notna(close_val):
                    hkd_rate = float(close_val)
        except Exception as e:
            logger.warning(f"yfinance 获取 HKD/CNY 失败，使用默认值：{type(e).__name__}")
    
    if hkd_rate:
        rates["HKD_CNY"] = round(hkd_rate, 4)
    
    return rates


def detect_ticker_currency(ticker: str) -> str:
    """
    根据股票代码特征判断其原生货币。
    
    Args:
        ticker: 股票代码（如 AAPL, 0700.HK, 600519.SS）
    
    Returns:
        str: 货币代码 "USD", "HKD", 或 "CNY"
    """
    ticker_upper = ticker.upper()
    
    if ".HK" in ticker_upper:
        return "HKD"
    elif ".SS" in ticker_upper or ".SZ" in ticker_upper:
        return "CNY"
    elif ticker_upper.replace(".", "").isalpha():
        return "USD"
    else:
        return "CNY"


def format_universal_ticker(ticker: str) -> str:
    """
    智能推断股票市场并格式化为 yfinance 识别的代码。
    
    Args:
        ticker: 原始股票代码（如 AAPL, 600519, 0700）
    
    Returns:
        str: 格式化后的 ticker（如 AAPL, 600519.SS, 0700.HK）
    """
    ticker = ticker.strip().upper()
    
    if "." in ticker:
        return ticker
        
    if ticker.isalpha():
        return ticker
        
    digits = ''.join(filter(str.isdigit, ticker))
    
    if len(digits) <= 5 and digits:
        return f"{str(int(digits)).zfill(4)}.HK"

    if len(digits) == 6:
        # 沪市：主板 60xxxx，科创板 68xxxx，沪市ETF 50xxxx/51xxxx/58xxxx
        if digits.startswith(('60', '68', '50', '51', '58')):
            return f"{digits}.SS"
        else:
            return f"{digits}.SZ"
            
    return ticker


def fetch_stock_price_raw(ticker: str, date: Optional[str] = None) -> Dict[str, Any]:
    """
    获取全球股票原始价格数据（支持美股/A 股/港股）。
    
    Args:
        ticker: 股票代码（如 AAPL, 600519, 0700）
        date: 可选日期 'YYYY-MM-DD'，未提供则返回最近交易日
    
    Returns:
        dict: {"open": xxx, "close": xxx, "date": "...", "high": xxx, "low": xxx}
    
    Raises:
        ValueError: 日期格式不正确
        KeyError: 数据字段缺失
        IndexError: 无历史数据
    """
    formatted_ticker = format_universal_ticker(ticker)
    stock = yf.Ticker(formatted_ticker)
    
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d")
            next_date = target_date + timedelta(days=1)
            hist = stock.history(
                start=target_date.strftime("%Y-%m-%d"),
                end=next_date.strftime("%Y-%m-%d"),
                timeout=10
            )
            date_label = date
        except ValueError as e:
            raise ValueError(f"日期格式不正确：{e}")
    else:
        hist = stock.history(period="1d", timeout=10)
        date_label = datetime.now().strftime("%Y-%m-%d")
    
    if hist.empty:
        raise IndexError(f"未找到 {formatted_ticker} 的历史数据")
    
    open_val = hist['Open'].iloc[0]
    close_val = hist['Close'].iloc[0]
    high_val = hist['High'].iloc[0] if 'High' in hist.columns else None
    low_val = hist['Low'].iloc[0] if 'Low' in hist.columns else None
    
    if pd.isna(open_val) or pd.isna(close_val):
        raise ValueError("数据不完整（存在空值）")
    
    actual_date = hist.index[0].strftime("%Y-%m-%d")
    
    result: Dict[str, Any] = {
        "ticker": formatted_ticker,
        "open": round(float(open_val), 2),
        "close": round(float(close_val), 2),
        "date": actual_date,
        "query_date": date_label
    }
    
    if high_val and not pd.isna(high_val):
        result["high"] = round(float(high_val), 2)
    if low_val and not pd.isna(low_val):
        result["low"] = round(float(low_val), 2)
    
    return result


def fetch_etf_price_raw(etf_code: str, date: Optional[str] = None) -> Dict[str, Any]:
    """
    获取 A 股 ETF 原始价格数据（yfinance + akshare 双源降级）。
    
    Args:
        etf_code: 6 位 ETF 代码（如 '513050'）
        date: 可选日期 'YYYY-MM-DD'，未提供则返回实时行情
    
    Returns:
        dict: {
            "etf_code": "513050",
            "open": xxx, "close": xxx, "high": xxx, "low": xxx,
            "volume": xxx, "date": "...",
            "source": "yfinance" | "akshare"
        }
    
    Raises:
        ValueError: ETF 代码格式不正确或数据不完整
        RuntimeError: 所有数据源均失败
    """
    etf_code = etf_code.strip()
    if not etf_code.isdigit() or len(etf_code) != 6:
        raise ValueError("ETF 代码格式不正确，请输入 6 位数字代码")
    
    if etf_code.startswith(('50', '51', '58')):
        suffix = '.SS'
    elif etf_code.startswith(('15', '16')):
        suffix = '.SZ'
    else:
        suffix = ''
    
    formatted_code = etf_code + suffix if suffix else etf_code
    
    yf_error: Optional[Exception] = None
    
    try:
        stock = yf.Ticker(formatted_code)
        
        if date:
            target_date = datetime.strptime(date, "%Y-%m-%d")
            next_date = target_date + timedelta(days=1)
            hist = stock.history(
                start=target_date.strftime("%Y-%m-%d"),
                end=next_date.strftime("%Y-%m-%d"),
                timeout=10
            )
            date_label = date
        else:
            hist = stock.history(period="1d", timeout=10)
            date_label = datetime.now().strftime("%Y-%m-%d")
        
        if not hist.empty:
            open_val = hist['Open'].iloc[0]
            close_val = hist['Close'].iloc[0]
            
            if pd.isna(open_val) or pd.isna(close_val):
                raise ValueError("yfinance 数据不完整")
            
            return {
                "etf_code": etf_code,
                "ticker": formatted_code,
                "open": round(float(open_val), 3),
                "close": round(float(close_val), 3),
                "high": round(float(hist['High'].iloc[0]), 3),
                "low": round(float(hist['Low'].iloc[0]), 3),
                "volume": int(hist['Volume'].iloc[0]),
                "date": hist.index[0].strftime("%Y-%m-%d"),
                "query_date": date_label,
                "source": "yfinance"
            }
    except Exception as e:
        yf_error = e
    
    try:
        if date:
            df = ak.fund_etf_hist_em(
                symbol=etf_code,
                period="daily",
                start_date=date.replace('-', ''),
                end_date=date.replace('-', ''),
                adjust=""
            )
            if df is None or df.empty:
                raise ValueError(f"akshare 未找到 {etf_code} 在 {date} 的数据")
            
            open_val = df['开盘'].iloc[0]
            close_val = df['收盘'].iloc[0]
            
            if pd.isna(open_val) or pd.isna(close_val):
                raise ValueError("akshare 数据不完整")
            
            return {
                "etf_code": etf_code,
                "open": round(float(open_val), 3),
                "close": round(float(close_val), 3),
                "high": round(float(df['最高'].iloc[0]), 3),
                "low": round(float(df['最低'].iloc[0]), 3),
                "volume": int(df['成交量'].iloc[0]),
                "date": date,
                "query_date": date,
                "source": "akshare_hist"
            }
        else:
            all_etfs = ak.fund_etf_spot_em()
            df = all_etfs[all_etfs['代码'] == etf_code]
            
            if df is None or df.empty:
                raise ValueError(f"akshare 未找到 ETF {etf_code} 的实时行情")
            
            return {
                "etf_code": etf_code,
                "current_price": round(float(df['最新价'].iloc[0]), 3),
                "open": round(float(df['今开'].iloc[0]), 3),
                "high": round(float(df['最高'].iloc[0]), 3),
                "low": round(float(df['最低'].iloc[0]), 3),
                "prev_close": round(float(df['昨收'].iloc[0]), 3),
                "change_percent": round(float(df['涨跌幅'].iloc[0]), 2),
                "volume": int(df['成交量'].iloc[0]),
                "amount": round(float(df['成交额'].iloc[0]) / 10000, 2),
                "date": datetime.now().strftime("%Y-%m-%d"),
                "query_date": "实时",
                "source": "akshare_spot"
            }
    except Exception as ak_error:
        if yf_error:
            raise RuntimeError(f"所有数据源失败：yfinance={yf_error}, akshare={ak_error}")
        raise RuntimeError(f"akshare 查询失败：{ak_error}")


def generate_kline_chart(ticker: str, save_dir: Path, days: int = 30) -> Dict[str, Any]:
    """
    生成股票 K 线走势图（支持自定义时间跨度）。
    
    Args:
        ticker: 股票代码
        save_dir: 图片保存目录
        days: K 线图的时间跨度（天数），默认 30 天
    
    Returns:
        dict: {
            "max_price": xxx,
            "min_price": xxx,
            "latest_close": xxx,
            "file_name": "...",
            "file_path": "..."
        }
    
    Raises:
        IndexError: 无历史数据
        KeyError: 数据字段缺失
    """
    formatted_ticker = format_universal_ticker(ticker)
    stock = yf.Ticker(formatted_ticker)
    
    # 计算起始日期
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    # 获取指定时间范围的历史数据
    hist = stock.history(start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"))
    
    if hist.empty:
        raise IndexError(f"未找到 {formatted_ticker} 的历史数据，无法绘图")
    
    safe_name = formatted_ticker.replace('.', '_')
    chart_filename = f"{safe_name}_30d_chart.png"
    chart_path = (save_dir / chart_filename).resolve()
    
    # 🌟 终极 UI 升级：TradingView 旗舰暗黑风格 (无边框悬浮质感)
    # 1. 极致色彩：采用 TradingView 官方经典的翠绿与猩红
    mc = mpf.make_marketcolors(
        up='#089981',             # TV 经典翠绿
        down='#f23645',           # TV 经典猩红
        edge='inherit',           # 边框与主体同色，消除发虚感
        wick='inherit',           # 影线与主体同色
        volume='in'               # 成交量颜色跟随 K 线涨跌
    )
    
    # 2. 旗舰质感：去边框化、深邃蓝灰背景、极度弱化网格
    s = mpf.make_mpf_style(
        marketcolors=mc,
        figcolor='#131722',       # TV 经典外框背景色（深邃蓝灰）
        facecolor='#131722',      # 图表内背景色（无缝融合）
        edgecolor='#131722',      # 隐藏坐标系边框，打造悬浮感
        gridcolor='#1e222d',      # 极度弱化的网格线
        gridstyle=':',            # 点状网格，绝不喧宾夺主
        rc={
            'text.color': '#d1d4dc',            # 柔和的银灰色文字
            'axes.labelcolor': '#d1d4dc',
            'xtick.color': '#d1d4dc',
            'ytick.color': '#d1d4dc',
            'axes.spines.top': False,           # 🔪 物理切除顶部边框
            'axes.spines.right': False,         # 🔪 物理切除右侧边框
            'axes.spines.left': False,          # 🔪 物理切除左侧边框
            'axes.spines.bottom': False,        # 🔪 物理切除底部边框
            'font.size': 10,
            'font.weight': 'bold',              # 字体加粗更具科技感
            'lines.linewidth': 1.5              # 稍微加粗均线，增加发光感
        }
    )

    # 3. 视网膜级输出 (400 DPI) 与自适应留白
    save_dict = dict(fname=str(chart_path), dpi=400, bbox_inches='tight', pad_inches=0.2)

    # 4. 宽屏渲染，注入定制的霓虹色均线
    mpf.plot(
        hist, 
        type='candle', 
        volume=True, 
        style=s, 
        title=f"\n{formatted_ticker} {days}-Day Trend", 
        mav=(5, 10),
        mavcolors=['#c481ec', '#2962ff'], # 🌟 视觉点睛：霓虹紫 + 电光蓝 均线
        figsize=(12, 6),                  # 🎬 升级为 2:1 的宽屏影院比例，K 线更舒展
        savefig=save_dict
    )
    
    max_price = round(float(hist['High'].max()), 2)
    min_price = round(float(hist['Low'].min()), 2)
    latest_close = round(float(hist['Close'].iloc[-1]), 2)
    
    return {
        "ticker": formatted_ticker,
        "max_price": max_price,
        "min_price": min_price,
        "latest_close": latest_close,
        "file_name": chart_filename,
        "file_path": str(chart_path)
    }


def _calculate_single_position(
    ticker: str,
    position: Dict[str, Any],
    exchange_rates: Dict[str, float]
) -> Dict[str, Any]:
    """
    计算单一持仓的市值与盈亏（内部纯函数，用于并发执行）。
    
    Args:
        ticker: 股票代码
        position: 持仓信息字典，包含 shares, cost_basis, type, company_name
        exchange_rates: 汇率字典
    
    Returns:
        dict: 包含该持仓的完整估值信息，如果发生异常则返回包含 "error" 的字典
    """
    shares = position.get("shares", 0)
    cost_basis = position.get("cost_basis", 0)
    company_name = position.get("company_name", "-")
    is_etf = position.get("type", "stock") == "etf"
    
    try:
        if is_etf:
            price_data = fetch_etf_price_raw(ticker)
            current_price = price_data.get("current_price", price_data.get("close"))
        else:
            price_data = fetch_stock_price_raw(ticker)
            current_price = price_data["close"]
        
        currency = detect_ticker_currency(ticker)
        exchange_rate = exchange_rates.get(f"{currency}_CNY", 1.0)
        
        native_market_value = current_price * shares
        native_cost_value = cost_basis * shares
        native_profit_loss = native_market_value - native_cost_value
        profit_loss_percent = (native_profit_loss / native_cost_value * 100) if native_cost_value != 0 else 0
        
        market_value_cny = native_market_value * exchange_rate
        cost_value_cny = native_cost_value * exchange_rate
        profit_loss_cny = market_value_cny - cost_value_cny
        
        currency_symbol = {"USD": "$", "HKD": "HK$", "CNY": "¥"}.get(currency, "¥")
        
        return {
            "ticker": ticker,
            "company_name": company_name,
            "shares": shares,
            "current_price": current_price,
            "currency": currency,
            "currency_symbol": currency_symbol,
            "exchange_rate": exchange_rate,
            "native_market_value": round(native_market_value, 2),
            "native_cost_value": round(native_cost_value, 2),
            "native_profit_loss": round(native_profit_loss, 2),
            "market_value_cny": round(market_value_cny, 2),
            "cost_value_cny": round(cost_value_cny, 2),
            "profit_loss_cny": round(profit_loss_cny, 2),
            "profit_loss_percent": round(profit_loss_percent, 2)
        }
        
    except Exception as e:
        return {
            "ticker": ticker,
            "company_name": company_name,
            "shares": shares,
            "error": f"获取价格失败：{type(e).__name__} - {str(e)}"
        }


def calculate_portfolio_valuation(positions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    计算持仓组合的精确总市值与今日总盈亏（统一折算为 CNY）。
    
    Args:
        positions: 持仓字典，格式如：
            {
                "AAPL": {"shares": 100, "cost_basis": 150.0},
                "600519": {"shares": 50, "cost_basis": 1800.0},
                "513050": {"shares": 1000, "cost_basis": 1.2, "type": "etf"}
            }
    
    Returns:
        dict: {
            "total_market_value": xxx,
            "total_cost": xxx,
            "total_profit_loss": xxx,
            "profit_loss_percent": xxx,
            "holdings": [
                {
                    "ticker": "AAPL",
                    "shares": 100,
                    "current_price": 175.0,
                    "market_value": 17500.0,
                    "profit_loss": 2500.0,
                    "profit_loss_percent": 16.67
                },
                ...
            ],
            "exchange_rates": {...},
            "currency_unit": "CNY"
        }
    """
    exchange_rates = fetch_exchange_rates()
    holdings_result = []
    total_market_value_cny = 0.0
    total_cost_cny = 0.0
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ticker = {
            executor.submit(_calculate_single_position, ticker, position, exchange_rates): ticker
            for ticker, position in positions.items()
        }
        
        for future in as_completed(future_to_ticker):
            ticker_key = future_to_ticker[future]
            try:
                result = future.result()
            except Exception as e:
                logger.error(f"持仓 {ticker_key} 估值计算失败：{type(e).__name__} - {e}")
                result = {"ticker": ticker_key, "error": str(e)}
            holdings_result.append(result)
            
            if "error" not in result:
                total_market_value_cny += result["market_value_cny"]
                total_cost_cny += result["cost_value_cny"]
    
    total_profit_loss_cny = total_market_value_cny - total_cost_cny
    total_profit_loss_percent = (total_profit_loss_cny / total_cost_cny * 100) if total_cost_cny != 0 else 0
    
    return {
        "total_market_value": round(total_market_value_cny, 2),
        "total_cost": round(total_cost_cny, 2),
        "total_profit_loss": round(total_profit_loss_cny, 2),
        "profit_loss_percent": round(total_profit_loss_percent, 2),
        "holdings": holdings_result,
        "exchange_rates": exchange_rates,
        "currency_unit": "CNY",
        "calculation_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


def parse_user_profile_to_positions(user_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    将用户持仓记忆文件（user_profile.json）中的自然语言持仓描述解析为标准 positions 格式。
    
    Args:
        user_data: 用户记忆字典，如：
            {
                "AAPL": "苹果公司，100 股，成本 200 美元/股",
                "风险偏好": "激进型",
                "513180": "10000 股，成本 0.677 元/股"
            }
    
    Returns:
        Dict[str, Dict[str, Any]]: 标准 positions 格式，如：
            {
                "AAPL": {"shares": 100, "cost_basis": 200.0, "type": "stock", "company_name": "苹果公司"},
                "513180": {"shares": 10000, "cost_basis": 0.677, "type": "etf", "company_name": "-"}
            }
    """
    import re
    
    positions = {}
    skip_keys = {"风险偏好", "投资目标", "备注", "持仓策略"}
    
    for key, value in user_data.items():
        if key in skip_keys:
            continue
        
        try:
            ticker = key
            holding_str = str(value)
            
            company_name = "-"
            parts = holding_str.replace('，', ',').split(',')
            if parts:
                first_part = parts[0].split(' ')[0].strip()
                if first_part and not re.match(r'^\d', first_part):
                    company_name = first_part
            
            shares_match = re.search(r'(\d+)\s*股', holding_str)
            cost_match = re.search(r'成本\s*([\d.]+)', holding_str)
            
            if not shares_match or not cost_match:
                continue
            
            shares = int(shares_match.group(1))
            cost_basis = float(cost_match.group(1))
            
            # ETF 前缀：沪市 50/51/58，深市 15/16，防止普通6位A股被误判
            is_etf = (key.isdigit() and len(key) == 6 and
                      key.startswith(('50', '51', '58', '15', '16')))
            
            positions[ticker] = {
                "shares": shares,
                "cost_basis": cost_basis,
                "type": "etf" if is_etf else "stock",
                "company_name": company_name
            }
        except Exception:
            continue
    
    return positions


def format_portfolio_report(valuation: Dict[str, Any]) -> str:
    """
    将 calculate_portfolio_valuation 返回的估值字典格式化为标准 Markdown 表格报告（多货币支持）。
    按持仓市值降序排列，优先展示重仓标的。
    
    Args:
        valuation: calculate_portfolio_valuation 返回的估值字典
    
    Returns:
        str: 格式化的 Markdown 报告字符串（包含标准表格）
    """
    exchange_rates = valuation.get("exchange_rates", DEFAULT_EXCHANGE_RATES)
    
    portfolio_details: List[Dict[str, Any]] = []
    
    for holding in valuation['holdings']:
        if 'error' in holding:
            portfolio_details.append({
                "ticker": holding['ticker'],
                "company_name": holding.get('company_name', '-'),
                "has_error": True,
                "error_message": holding['error']
            })
        else:
            currency_symbol = holding.get('currency_symbol', '¥')
            current_price = holding.get('current_price', 0)
            shares = holding.get('shares', 1)
            native_cost_value = holding.get('native_cost_value', 0)
            cost_basis = native_cost_value / shares if shares > 0 else 0
            native_value = holding.get('native_market_value', 0)
            cny_value = holding.get('market_value_cny', 0)
            cny_profit = holding.get('profit_loss_cny', 0)
            pnl_percent = holding.get('profit_loss_percent', 0)
            
            portfolio_details.append({
                "ticker": holding['ticker'],
                "company_name": holding.get('company_name', '-'),
                "has_error": False,
                "currency_symbol": currency_symbol,
                "current_price": current_price,
                "cost_basis": cost_basis,
                "native_value": native_value,
                "cny_value": cny_value,
                "cny_profit": cny_profit,
                "pnl_percent": pnl_percent
            })
    
    sorted_details = sorted(
        portfolio_details,
        key=lambda x: x.get('cny_value', 0) if not x.get('has_error', False) else -1,
        reverse=True
    )
    
    markdown_lines = [
        "## 💰 持仓市值与盈亏对账单",
        "",
        f"**计算时间**: {valuation['calculation_time']}",
        "",
        f"**参考汇率**: USD/CNY={exchange_rates.get('USD_CNY', 7.20):.4f}, HKD/CNY={exchange_rates.get('HKD_CNY', 0.92):.4f}",
        "",
        "### 📊 总资产概览",
        "",
        f"- **总市值**: ¥{valuation['total_market_value']:,.2f}",
        f"- **总成本**: ¥{valuation['total_cost']:,.2f}",
        f"- **累计盈亏**: ¥{valuation['total_profit_loss']:,.2f} ({valuation['profit_loss_percent']:+.2f}%)",
        "",
        "### 📈 持仓明细",
        "",
        "| 标的代码 | 公司名称 | 最新价 | 持仓成本 | 原生市值 | 折合人民币 (CNY) | 绝对盈亏 (CNY) | 盈亏率 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    
    for detail in sorted_details:
        if detail.get('has_error', False):
            markdown_lines.append(
                f"| **{detail['ticker']}** | {detail['company_name']} | ❌ {detail['error_message']} | - | - | - | - | - |"
            )
        else:
            currency_symbol = detail.get('currency_symbol', '¥')
            current_price = detail.get('current_price', 0)
            cost_basis = detail.get('cost_basis', 0)
            native_value = detail.get('native_value', 0)
            cny_value = detail.get('cny_value', 0)
            cny_profit = detail.get('cny_profit', 0)
            pnl_percent = detail.get('pnl_percent', 0)
            
            markdown_lines.append(
                f"| {detail['ticker']} | {detail['company_name']} | {currency_symbol}{current_price:.2f} | {currency_symbol}{cost_basis:.2f} | {currency_symbol}{native_value:,.2f} | ¥{cny_value:,.2f} | {cny_profit:+,.2f} | {pnl_percent:+.2f}% |"
            )
    
    summary_line = f"**【账户总计】当前折合总市值：¥{valuation['total_market_value']:,.2f}，累计总盈亏：{valuation['total_profit_loss']:+,.2f}**"
    
    markdown_lines.extend([
        "",
        summary_line
    ])
    
    return "\n".join(markdown_lines)

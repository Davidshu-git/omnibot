"""
盘后调度主程序 - 每日盘后热点分析与邮件推送调度器。

本模块负责：
1. 定时调度（每日 16:30 盘后，覆盖港股 16:10 收盘竞价）
2. 高可用多源数据聚合（财联社/新浪/东财并行拉取 + 去重融合）
3. 读取用户持仓记忆
4. 调用大模型生成盘后报告
5. 发送邮件推送
"""

import socket
import os
import sys
import json
import time
import threading
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, TypedDict

import schedule
import akshare as ak
import pandas as pd
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langchain_core.prompts import ChatPromptTemplate
from pydantic import SecretStr
from dotenv import load_dotenv
from rich.console import Console
from tenacity import retry, stop_after_attempt, wait_exponential

from core.notifier import send_market_report_email
from core.model_registry import make_standard_registry

socket.setdefaulttimeout(30)
from stock_bot.valuation_engine import (
    PROFILE_SKIP_KEYS,
    TICKER_KEY_PATTERN,
    fetch_stock_price_raw,
    normalize_cash_platform,
    parse_user_profile_to_positions,
    parse_cash_assets,
    calculate_portfolio_valuation,
    format_portfolio_report,
)


console: Console = Console()

BASE_DIR: Path = Path(__file__).resolve().parent.parent
STOCK_MEMORY_DIR: Path = BASE_DIR / "data/stock/memory"
STOCK_KB_DIR: Path = BASE_DIR / "data/stock/knowledge_base"
STOCK_WORKSPACE_DIR: Path = BASE_DIR / "data/stock/agent_workspace"

load_dotenv()


class ReportState(TypedDict):
    """多智能体共享的会议桌状态结构"""
    user_memory: str
    indices_data: str
    portfolio_metrics: Dict[str, Any]
    portfolio_context: Dict[str, Any]
    market_context: Dict[str, Any]
    data_quality: Dict[str, Any]
    advice_policy: Dict[str, Any]
    portfolio_analysis: str
    market_analysis: str
    risk_analysis: str
    final_report: str


def fetch_global_indices() -> str:
    """
    抓取全球核心指数当日涨跌幅数据。

    使用 valuation_engine.fetch_stock_price_raw 获取三大核心指数（沪深 300、恒生指数、纳斯达克 100）的当日行情，
    计算涨跌幅百分比，并提供降级容错机制。

    Returns:
        str: 格式化后的指数涨跌幅文本，格式如：
             "【今日核心指数】沪深 300: +1.25%, 恒生指数：-0.50%, 纳斯达克 100: +0.88%"
    """
    indices_config: Dict[str, Dict[str, str]] = {
        "沪深 300": {"ticker": "000300.SS", "name": "沪深 300"},
        "恒生指数": {"ticker": "^HSI", "name": "恒生指数"},
        "恒生科技指数": {"ticker": "HSTECH.HK", "name": "恒生科技指数"},
        "纳斯达克 100": {"ticker": "^NDX", "name": "纳斯达克 100"},
    }

    results: List[str] = []

    for config in indices_config.values():
        ticker: str = config["ticker"]
        name: str = config["name"]

        try:
            price_data = fetch_stock_price_raw(ticker)
            open_price: float = price_data["open"]
            close_price: float = price_data["close"]

            if open_price != 0:
                change_pct: float = ((close_price - open_price) / open_price) * 100
                sign: str = "+" if change_pct >= 0 else ""
                results.append(f"{name}: {sign}{change_pct:.2f}%")
            else:
                results.append(f"{name}: 获取失败")
        except Exception:
            results.append(f"{name}: 获取失败")

    return f"【今日核心指数】{', '.join(results)}"


def load_user_profile() -> Dict[str, Any]:
    """
    读取用户持仓与偏好记忆文件。

    Returns:
        Dict[str, Any]: 用户记忆字典，如果文件不存在或解析失败则返回空字典。
    """
    profile_path: Path = STOCK_MEMORY_DIR / "user_profile.json"

    if not profile_path.exists():
        return {}

    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            data: dict = json.load(f)

        if not data:
            return {}

        return data
    except json.JSONDecodeError:
        return {}
    except Exception:
        return {}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
def _fetch_cls_news() -> pd.DataFrame:
    return ak.stock_info_global_cls(symbol="全部")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
def _fetch_sina_news() -> pd.DataFrame:
    return ak.stock_info_global_sina()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
def _fetch_em_news() -> pd.DataFrame:
    return ak.stock_info_global_em()


_MIN_NEWS_ITEMS: int = 30
"""akshare 财经源（财联社/新浪/东财）聚合后的最低条数阈值。

低于此值视为财经源集体失效/产出过少，启用 DuckDuckGo 搜索兜底补充。正常情况下
新浪(~20)+东财(~200)足以越过阈值，兜底不触发，避免引入搜索结果噪声。"""


def _fetch_ddgs_news() -> pd.DataFrame:
    """DuckDuckGo 财经新闻搜索兜底源。

    当 akshare 财经源集体失效时启用。打 duckduckgo（经代理出网），故障域与 akshare
    （境内东财/新浪）完全独立。多查询拼接，单查询失败不影响其余。

    Returns:
        pd.DataFrame: 含 ``time`` / ``content`` 两列（已对齐聚合 schema）；无结果返回空表。
    """
    from ddgs import DDGS

    queries = ["A股 港股 财经 快讯", "美股 全球股市 要闻", "stock market news today"]
    rows: List[Dict[str, str]] = []
    ddgs = DDGS(timeout=8)
    for q in queries:
        try:
            for item in ddgs.news(q, max_results=10):
                title = (item.get("title") or "").strip()
                if title:
                    rows.append({"time": str(item.get("date", "")), "content": title})
        except Exception as exc:  # noqa: BLE001 - 单查询失败跳过，不影响兜底整体
            console.print(f"[dim]   └─ DDGS 查询 {q!r} 失败：{type(exc).__name__}[/dim]")
            continue

    return pd.DataFrame(rows, columns=["time", "content"])


_SOURCE_TIMEOUT: int = 20
"""单个资讯源的硬超时（秒）。

akshare 底层 requests/urllib3 会用自身的 socket 超时覆盖 ``socket.setdefaulttimeout``，
某一源（如财联社）服务端接受连接却不回包时会无限阻塞 read，导致整份日报永远卡死。
故在线程级施加硬超时：超时即放弃该源（守护线程随主进程退出），保证其余源照常产出报告。
"""


def fetch_global_market_news() -> str:
    """
    高可用多源数据聚合抓取，内置三级降级与数据融合机制。

    并行拉取以下三个宏观资讯源：
    1. 财联社全球电报 (ak.stock_info_global_cls) - 列名：['标题', '内容', '发布日期', '发布时间']
    2. 新浪 7x24 快讯 (ak.stock_info_global_sina) - 列名：['时间', '内容']
    3. 东财全球快讯 (ak.stock_info_global_em) - 列名：['标题', '摘要', '发布时间', '链接']

    Returns:
        str: 合并去重后的资讯文本（最多 200 条）。

    Raises:
        RuntimeError: 所有数据源全部失效时抛出。
    """
    data_sources: List[Dict[str, Any]] = [
        # 财联社 stock_info_global_cls 服务端持续吊死（接受连接不回包，非网络问题），
        # 每次都要等满 _SOURCE_TIMEOUT 才跳过、平白拖慢 20s。暂时摘除，待 akshare 升级
        # 或财联社接口恢复后取消注释即可恢复（函数 _fetch_cls_news 与映射均保留）。
        # {"name": "财联社全球电报", "func": _fetch_cls_news},
        {"name": "新浪 7x24", "func": _fetch_sina_news},
        {"name": "东财全球快讯", "func": _fetch_em_news},
    ]

    column_mapping: Dict[str, Dict[str, str]] = {
        "财联社全球电报": {"time": "发布时间", "content": "内容"},  # 摘除中，恢复财联社时一并启用
        "新浪 7x24": {"time": "时间", "content": "内容"},
        "东财全球快讯": {"time": "发布时间", "content": "摘要"},
    }

    # 并行启动三源抓取（守护线程），主线程以共享 deadline 逐个 join，单源最多等
    # _SOURCE_TIMEOUT 秒。超时的源继续在后台跑（daemon，随进程退出），主流程不被其拖死。
    results: Dict[str, Any] = {}

    def _runner(name: str, fn: Any) -> None:
        """在守护线程中执行单源抓取，结果或异常统一回填 results。"""
        try:
            results[name] = fn()
        except Exception as exc:  # noqa: BLE001 - 记录后由主线程统一降级，不可让子线程异常逃逸
            results[name] = exc

    threads: Dict[str, threading.Thread] = {}
    for source in data_sources:
        th = threading.Thread(
            target=_runner,
            args=(source["name"], source["func"]),
            name=f"news-{source['name']}",
            daemon=True,
        )
        th.start()
        threads[source["name"]] = th

    deadline: float = time.monotonic() + _SOURCE_TIMEOUT

    dfs = []

    for source in data_sources:
        source_name: str = source["name"]
        threads[source_name].join(max(0.0, deadline - time.monotonic()))

        if threads[source_name].is_alive():
            console.print(f"[bold red]❌ {source_name} 抓取超时（> {_SOURCE_TIMEOUT}s），跳过[/bold red]")
            continue

        outcome = results.get(source_name)
        if isinstance(outcome, Exception):
            console.print(f"[bold red]❌ {source_name} 获取失败：{type(outcome).__name__} - {str(outcome)}[/bold red]")
            continue

        df: pd.DataFrame = outcome

        if df is not None and not df.empty:
            console.print(f"[bold green]✅ 成功获取 {source_name}: {len(df)} 条数据[/bold green]")

            mapping = column_mapping.get(source_name, {})
            time_col: str = mapping.get("time", "")
            content_col: str = mapping.get("content", "")

            if time_col in df.columns and content_col in df.columns:
                selected_df = df[[time_col, content_col]].copy()
                selected_df.columns = ["time", "content"]
                dfs.append(selected_df)
                console.print(f"[dim]   └─ 列名映射：{time_col} → time, {content_col} → content[/dim]")
            else:
                console.print(f"[bold red]❌ {source_name} 列名不匹配，期望 time='{time_col}', content='{content_col}'[/bold red]")
                console.print(f"[dim]   实际列名：{list(df.columns)}[/dim]")
                continue
        else:
            console.print(f"[bold yellow]⚠️  {source_name} 返回空数据[/bold yellow]")

    # 搜索引擎兜底：akshare 财经源集体失效/产出过少时，用 DuckDuckGo 补充全球财经要闻。
    akshare_items: int = sum(len(d) for d in dfs)
    if akshare_items < _MIN_NEWS_ITEMS:
        console.print(
            f"[bold yellow]⚠️  akshare 财经源仅 {akshare_items} 条（< {_MIN_NEWS_ITEMS}），"
            f"启用 DuckDuckGo 搜索兜底[/bold yellow]"
        )
        ddgs_box: Dict[str, Any] = {}

        def _ddgs_runner() -> None:
            try:
                ddgs_box["df"] = _fetch_ddgs_news()
            except Exception as exc:  # noqa: BLE001 - 兜底失败不影响主流程
                ddgs_box["err"] = exc

        ddgs_thread = threading.Thread(target=_ddgs_runner, name="news-ddgs", daemon=True)
        ddgs_thread.start()
        ddgs_thread.join(_SOURCE_TIMEOUT)

        if ddgs_thread.is_alive():
            console.print(f"[bold red]❌ DuckDuckGo 兜底超时（> {_SOURCE_TIMEOUT}s）[/bold red]")
        elif "err" in ddgs_box:
            console.print(f"[bold red]❌ DuckDuckGo 兜底失败：{type(ddgs_box['err']).__name__}[/bold red]")
        else:
            ddgs_df = ddgs_box.get("df")
            if ddgs_df is not None and not ddgs_df.empty:
                dfs.append(ddgs_df[["time", "content"]].copy())
                console.print(f"[bold green]✅ DuckDuckGo 兜底补充 {len(ddgs_df)} 条[/bold green]")

    if not dfs:
        raise RuntimeError("所有宏观资讯源全部失效")

    merged_df: pd.DataFrame = pd.concat(dfs, ignore_index=True)

    deduped_df: pd.DataFrame = merged_df.drop_duplicates(subset=["content"]).copy()

    if "time" in deduped_df.columns:
        try:
            deduped_df.loc[:, "time"] = pd.to_datetime(deduped_df["time"], errors="coerce")
            deduped_df = deduped_df.sort_values("time", ascending=False).reset_index(drop=True)
            console.print("[dim]   └─ 时间排序成功（已转换为 datetime）[/dim]")
        except Exception as e:
            console.print(f"[yellow dim]⚠️  时间排序失败：{type(e).__name__}，使用原始顺序[/yellow dim]")

    final_df: pd.DataFrame = deduped_df.head(200).copy()

    console.print(f"[bold cyan]📊 多源聚合去重完成，最终采用 {len(final_df)} 条有效资讯进行推理。[/bold cyan]")

    news_items: List[str] = []
    for _, row in final_df.iterrows():
        time_str: str = str(row.get("time", ""))
        content_str: str = str(row.get("content", ""))
        if content_str.strip():
            news_items.append(f"[{time_str}] {content_str}")

    return "\n".join(news_items)


def _round_money(value: Any) -> float:
    """把数值安全转成两位小数 float。

    Args:
        value: 任意可能为数值的对象。

    Returns:
        float: 转换失败返回 0.0。
    """
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _pct(part: float, total: float) -> float:
    """计算百分比，分母为 0 时返回 0。

    Args:
        part: 分子。
        total: 分母。

    Returns:
        float: 百分比，两位小数。
    """
    return round(part / total * 100, 2) if total else 0.0


def audit_user_profile_parsing(
    user_data: Dict[str, Any],
    positions: Dict[str, Dict[str, Any]],
    cash_assets: List[Dict[str, Any]],
) -> List[str]:
    """审计用户记忆里疑似持仓/现金但未被解析的条目。

    Args:
        user_data: 原始 user_profile.json 内容。
        positions: 已成功解析出的持仓。
        cash_assets: 已成功解析出的现金条目。

    Returns:
        List[str]: 数据质量警告。为空表示未发现明显解析风险。
    """
    warnings: List[str] = []
    parsed_cash_platforms = {str(c.get("platform", "")) for c in cash_assets}
    # 教训/纠错类记忆经常复述"成本/股数"字样（如"曾把总成本当单价"），
    # 不是持仓条目，不能触发漏算告警把日报打成 restricted。
    lesson_markers = ("教训", "纠错", "错误", "复盘", "提醒")

    for key, raw_value in user_data.items():
        key_str = str(key)
        value = str(raw_value)
        if key_str in PROFILE_SKIP_KEYS or any(m in key_str for m in lesson_markers):
            continue

        if key_str.startswith("现金"):
            if normalize_cash_platform(key_str) not in parsed_cash_platforms:
                warnings.append(f"现金条目可能未解析：{key_str} -> {value}")
            continue

        looks_like_position = (
            "成本" in value and any(unit in value for unit in ("股", "枚", "个"))
        )
        if looks_like_position and key_str not in positions:
            reason = "key 非标准 ticker" if not TICKER_KEY_PATTERN.match(key_str) else "value 格式不符合解析器"
            warnings.append(f"疑似持仓未入估值：{key_str} -> {value}（{reason}）")

    return warnings


def build_portfolio_context(
    valuation: Dict[str, Any],
    profile_warnings: List[str],
) -> Dict[str, Any]:
    """把估值结果整理成 PM 可用的结构化组合画像。

    Args:
        valuation: calculate_portfolio_valuation 的返回值。
        profile_warnings: 用户记忆解析审计警告。

    Returns:
        Dict[str, Any]: 包含仓位、分类、现金、风险集中度等结构化字段。
    """
    total_value = _round_money(valuation.get("total_market_value", 0.0))
    cash_total = _round_money(valuation.get("cash_total_cny", 0.0))
    holdings = valuation.get("holdings", [])
    successful_holdings = [h for h in holdings if isinstance(h, dict) and "error" not in h]
    errored_holdings = [h for h in holdings if isinstance(h, dict) and h.get("error")]

    enriched_holdings: List[Dict[str, Any]] = []
    asset_groups: Dict[str, float] = {"stock": 0.0, "etf": 0.0, "crypto": 0.0, "cash": cash_total}
    currency_exposure: Dict[str, float] = {}

    for h in successful_holdings:
        market_value = _round_money(h.get("market_value_cny", 0.0))
        asset_type = str(h.get("type", "stock"))
        asset_groups[asset_type] = asset_groups.get(asset_type, 0.0) + market_value

        currency = str(h.get("currency", "CNY"))
        if asset_type != "crypto":
            currency_exposure[currency] = currency_exposure.get(currency, 0.0) + market_value

        enriched_holdings.append({
            "ticker": h.get("ticker"),
            "company_name": h.get("company_name", "-"),
            "type": asset_type,
            "currency": currency,
            "market_value_cny": market_value,
            "weight_pct": _pct(market_value, total_value),
            "cost_value_cny": _round_money(h.get("cost_value_cny", 0.0)),
            "profit_loss_cny": _round_money(h.get("profit_loss_cny", 0.0)),
            "profit_loss_percent": _round_money(h.get("profit_loss_percent", 0.0)),
            "current_price": h.get("current_price"),
            "suspect": bool(h.get("suspect")),
        })

    for cash in valuation.get("cash_holdings", []):
        if not isinstance(cash, dict):
            continue
        currency = str(cash.get("currency", "CNY"))
        currency_exposure[currency] = currency_exposure.get(currency, 0.0) + _round_money(cash.get("cny_value", 0.0))

    enriched_holdings.sort(key=lambda x: x["market_value_cny"], reverse=True)
    asset_group_weights = {
        k: {"value_cny": round(v, 2), "weight_pct": _pct(v, total_value)}
        for k, v in sorted(asset_groups.items())
        if abs(v) > 0.01
    }

    max_holding = enriched_holdings[0] if enriched_holdings else None
    top3_weight = round(sum(h["weight_pct"] for h in enriched_holdings[:3]), 2)
    suspect_tickers = [str(h.get("ticker")) for h in successful_holdings if h.get("suspect")]

    risk_flags: List[str] = []
    if max_holding and max_holding["weight_pct"] >= 35:
        risk_flags.append(f"单一标的 {max_holding['ticker']} 占比 {max_holding['weight_pct']:.2f}%，集中度偏高")
    if top3_weight >= 70:
        risk_flags.append(f"前三大持仓合计占比 {top3_weight:.2f}%，组合分散度不足")
    if _pct(cash_total, total_value) < 5 and total_value > 0:
        risk_flags.append("现金比例低于 5%，调仓/补仓弹性不足")
    crypto_weight = asset_group_weights.get("crypto", {}).get("weight_pct", 0.0)
    if crypto_weight >= 20:
        risk_flags.append(f"加密货币占比 {crypto_weight:.2f}%，波动敞口较高")

    return {
        "total_market_value_cny": total_value,
        "total_cost_cny": _round_money(valuation.get("total_cost", 0.0)),
        "total_profit_loss_cny": _round_money(valuation.get("total_profit_loss", 0.0)),
        "profit_loss_percent": _round_money(valuation.get("profit_loss_percent", 0.0)),
        "cash_total_cny": cash_total,
        "cash_weight_pct": _pct(cash_total, total_value),
        "asset_groups": asset_group_weights,
        "currency_exposure": {
            k: {"value_cny": round(v, 2), "weight_pct": _pct(v, total_value)}
            for k, v in sorted(currency_exposure.items())
        },
        "currency_exposure_note": "加密货币作为独立资产类别统计在 asset_groups，不计入币种敞口，故各币种加总可能小于总市值。",
        "holdings": enriched_holdings,
        "top_holdings": enriched_holdings[:5],
        "max_single_position": max_holding,
        "top3_weight_pct": top3_weight,
        "errored_tickers": [str(h.get("ticker", "UNKNOWN")) for h in errored_holdings],
        "suspect_tickers": suspect_tickers,
        "profile_warnings": profile_warnings,
        "risk_flags": risk_flags,
    }


def build_market_context(news_text: str, indices_data: str) -> Dict[str, Any]:
    """整理市场新闻上下文，避免 PM 直接被 200 条快讯淹没。

    Args:
        news_text: 已聚合去重的新闻文本。
        indices_data: 核心指数文本。

    Returns:
        Dict[str, Any]: 新闻数量、核心指数、精选头条等。
    """
    news_lines = [line.strip() for line in news_text.splitlines() if line.strip()]
    # 新闻按时间倒序排列，只取头部会偏向收盘前最后一两个小时的快讯、
    # 丢掉全天叙事，故超量时按等距抽样覆盖整个时间范围。
    max_sample = 80
    if len(news_lines) <= max_sample:
        headline_sample = news_lines
    else:
        step = len(news_lines) / max_sample
        headline_sample = [news_lines[int(i * step)] for i in range(max_sample)]
    return {
        "indices": indices_data,
        "news_count": len(news_lines),
        "headline_sample": headline_sample,
        "sample_note": "样本按时间等距抽取以覆盖全天，非仅最新快讯。",
        "source_note": "新闻来自新浪/东财财经快讯；当主源产出不足时可能混入 DuckDuckGo 兜底结果。",
    }


def build_data_quality(
    market_context: Dict[str, Any],
    portfolio_context: Dict[str, Any],
) -> Dict[str, Any]:
    """根据行情、新闻、估值结果生成数据质量门禁。

    Args:
        market_context: build_market_context 的结果。
        portfolio_context: build_portfolio_context 的结果。

    Returns:
        Dict[str, Any]: status 为 ok/restricted。
    """
    issues: List[str] = []
    restrictions: List[str] = []

    if market_context.get("news_count", 0) < _MIN_NEWS_ITEMS:
        issues.append(f"新闻样本仅 {market_context.get('news_count', 0)} 条，市场叙事可信度下降")
        restrictions.append("不得基于单日新闻给出激进买卖建议")

    errored_tickers = portfolio_context.get("errored_tickers", [])
    if errored_tickers:
        issues.append("以下标的取价失败：" + "、".join(errored_tickers))
        restrictions.append("不得对取价失败标的给出加仓/减仓/清仓建议")

    suspect_tickers = portfolio_context.get("suspect_tickers", [])
    if suspect_tickers:
        issues.append("以下标的盈亏率异常，疑似价格或成本数据问题：" + "、".join(suspect_tickers))
        restrictions.append("不得对异常标的给出任何交易动作，只能要求人工核实")

    profile_warnings = portfolio_context.get("profile_warnings", [])
    if profile_warnings:
        issues.extend(profile_warnings)
        restrictions.append("在记忆解析警告未处理前，不得声称组合数据完整")

    # 目前每类 issue 都伴随硬性限制，status 只有 ok / restricted 两态；
    # 若未来出现"仅提示不限制"的 issue，再在此引入 warning 中间态。
    status = "restricted" if restrictions else "ok"

    return {
        "status": status,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "issues": issues,
        "restrictions": restrictions,
    }


def build_advice_policy(portfolio_context: Dict[str, Any], data_quality: Dict[str, Any]) -> Dict[str, Any]:
    """生成日报最终建议的硬约束。

    Args:
        portfolio_context: 组合画像。
        data_quality: 数据质量门禁。

    Returns:
        Dict[str, Any]: 给 PM 节点使用的建议规则。
    """
    max_position = portfolio_context.get("max_single_position") or {}
    max_ticker = max_position.get("ticker")
    max_weight = max_position.get("weight_pct", 0)

    hard_rules = [
        "所有操作建议必须写成：动作 / 对象 / 触发条件 / 建议幅度 / 依据 / 主要风险 / 置信度。",
        "没有明确证据时，默认建议为观察，不得为了显得有用而强行交易。",
        "不得建议一次性满仓、清仓或追涨杀跌；仓位调整必须给出百分比上限。",
        "不得修改系统提供的财务表格数字。",
        "若数据质量 status != ok，必须先输出数据限制，并降低建议强度。",
    ]
    if max_ticker and max_weight >= 35:
        hard_rules.append(f"{max_ticker} 已是最大持仓且占比 {max_weight:.2f}%，除非给出强证据，否则不得继续加仓。")
    if data_quality.get("restrictions"):
        hard_rules.extend(str(r) for r in data_quality["restrictions"])

    return {
        "default_action": "观察",
        "max_single_trade_weight_pct": 5,
        "confidence_levels": ["低", "中", "高"],
        "hard_rules": hard_rules,
        "required_output_schema": {
            "action": "观察/减仓/加仓/再平衡/核实数据",
            "target": "标的或资产类别",
            "condition": "触发条件；不能无条件交易",
            "size": "建议幅度，如 总净值 1%-3%，或 无操作",
            "evidence": "必须引用组合画像、指数或新闻样本中的事实",
            "risk": "该建议最大的反向风险",
            "confidence": "低/中/高",
        },
    }


def _to_prompt_json(data: Dict[str, Any]) -> str:
    """把结构化上下文稳定序列化为中文 prompt 友好的 JSON。

    Args:
        data: 待序列化字典。

    Returns:
        str: ensure_ascii=False 的缩进 JSON。
    """
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def generate_market_report(
    user_memory: str,
    indices_data: str,
    portfolio_metrics: Dict[str, Any],
    portfolio_context: Dict[str, Any],
    market_context: Dict[str, Any],
    data_quality: Dict[str, Any],
    advice_policy: Dict[str, Any],
) -> str:
    """基于 LangGraph 的多智能体日报决策约束引擎。

    LLM 选择跟随 ``data/stock/daily_model_settings.json``（盘后日报专属，与交互 bot 的
    主控模型 ``model_settings.json`` 解耦）。在 obs 主界面「日报模型」chip 或 Telegram
    切换后，下一次盘后报告自动生效，无需改代码或重启本进程。
    """
    settings_dir: Path = (Path(__file__).parent / "../data/stock").resolve()
    registry = make_standard_registry(
        settings_dir, settings_filename="daily_model_settings.json", default_key="deepseek"
    )
    cfg = registry.current()
    if not cfg.api_key:
        raise ValueError(f"当前选中模型 {cfg.key!r}（{cfg.display_name}）的 API Key 未配置")

    def _build_llm(temperature: float) -> ChatOpenAI:
        """以当前选中模型为基底构建 LLM，按节点覆盖温度。

        Args:
            temperature: 该节点期望的采样温度。

        Returns:
            构建好的 ChatOpenAI（或其子类，如 DeepSeekChatLLM）实例。

        Note:
            若该模型不支持温度（``cfg.temperature is None``，如思考模式），则不传温度，
            避免触发 provider 的参数报错。
        """
        kwargs: Dict[str, Any] = dict(
            model=cfg.model,
            base_url=cfg.base_url,
            api_key=SecretStr(cfg.api_key),
            timeout=120,
            max_retries=3,
            max_tokens=8192,
        )
        if cfg.temperature is not None:
            kwargs["temperature"] = temperature
        if cfg.extra_body:
            kwargs["extra_body"] = cfg.extra_body
        return cfg.llm_class(**kwargs)

    analyst_llm = _build_llm(0.2)  # 分析节点：低温，强调事实提炼而非情绪发散
    risk_llm = _build_llm(0.1)     # 风控节点：低温，强调约束和禁止动作
    pm_llm = _build_llm(0.1)       # PM 节点：保守收敛，输出可执行但受限的建议

    def portfolio_node(state: ReportState):
        console.print("[bold green]📊 [Agent 1] 组合分析师正在计算仓位画像与盈亏归因...[/bold green]")
        prompt = ChatPromptTemplate.from_template(
            "你是组合分析师，只能基于【结构化组合画像】和【精准财务表格】分析，不得编造估值、目标价或新闻。\n"
            "输出要求：\n"
            "1. 列出组合最重要的 3 个事实：仓位集中度、现金比例、资产类别/币种敞口。\n"
            "2. 解释累计盈亏主要来自哪些持仓或资产类别。\n"
            "3. 标出需要 PM 注意的组合层面风险。\n"
            "4. 不给买卖建议，只给事实判断。限 450 字。\n\n"
            "【结构化组合画像 JSON】\n{portfolio_context_json}\n\n"
            "【精准财务表格】\n{markdown_report}"
        )
        chain = prompt | analyst_llm
        res = chain.invoke({
            "portfolio_context_json": _to_prompt_json(state["portfolio_context"]),
            "markdown_report": state["portfolio_metrics"].get("markdown_report", "暂无明细数据"),
        })
        return {"portfolio_analysis": res.content}

    def market_node(state: ReportState):
        console.print("[bold cyan]🌍 [Agent 2] 市场分析师正在提炼指数与新闻相关性...[/bold cyan]")
        prompt = ChatPromptTemplate.from_template(
            "你是市场分析师。你的任务是把市场信息和用户组合相关联，而不是写泛泛宏观评论。\n"
            "输出要求：\n"
            "1. 总结核心指数表现。\n"
            "2. 从新闻样本中提炼最多 5 条与用户持仓/资产类别可能相关的线索。\n"
            "3. 明确说明哪些新闻只是背景噪声，不足以支持交易。\n"
            "4. 不给买卖建议，只给市场证据强弱判断。限 500 字。\n\n"
            "【市场上下文 JSON】\n{market_context_json}\n\n"
            "【用户长期记忆】\n{user_memory}\n\n"
            "【结构化组合画像 JSON】\n{portfolio_context_json}"
        )
        chain = prompt | analyst_llm
        res = chain.invoke({
            "market_context_json": _to_prompt_json(state["market_context"]),
            "user_memory": state["user_memory"],
            "portfolio_context_json": _to_prompt_json(state["portfolio_context"]),
        })
        return {"market_analysis": res.content}

    def risk_node(state: ReportState):
        console.print("[bold red]🛡️ [Agent 3] 风控官正在生成建议边界和禁止动作...[/bold red]")
        prompt = ChatPromptTemplate.from_template(
            "你是风控官。你的任务不是预测涨跌，而是规定今天哪些建议可以给、哪些必须禁止。\n"
            "输出要求：\n"
            "1. 先检查 data_quality.status；若不是 ok，必须列出限制。\n"
            "2. 根据组合画像列出风险预算：单笔最大调仓、是否允许加仓最大持仓、是否允许处理异常标的。\n"
            "3. 给 PM 一组明确的禁止动作和允许动作。\n"
            "4. 不写情绪化语言。限 450 字。\n\n"
            "【数据质量 JSON】\n{data_quality_json}\n\n"
            "【建议策略约束 JSON】\n{advice_policy_json}\n\n"
            "【结构化组合画像 JSON】\n{portfolio_context_json}"
        )
        chain = prompt | risk_llm
        res = chain.invoke({
            "data_quality_json": _to_prompt_json(state["data_quality"]),
            "advice_policy_json": _to_prompt_json(state["advice_policy"]),
            "portfolio_context_json": _to_prompt_json(state["portfolio_context"]),
        })
        return {"risk_analysis": res.content}

    def pm_node(state: ReportState):
        console.print("[bold magenta]👨‍⚖️ [Agent 4] 投资总监正在按风控约束生成最终日报...[/bold magenta]")
        system_prompt = """你是一位顶级的华尔街投资总监（PM）。你需要审视组合分析师、市场分析师和风控官的结论，结合用户的【精准财务明细】，输出最终的盘后研报。

你的真正职责不是每天强行交易，而是把市场信息、组合画像、数据质量和风险预算整合成可执行但克制的投资备忘录。

你的回复必须严格采用 Markdown 格式，并强制包含以下四大核心模块：

### 1. 🌍 市场证据与今日结论
- 综合今日核心指数表现。
- 只保留和用户组合有关的市场线索；明确区分"强证据"与"背景噪声"。
- 给出今日总判断：进攻 / 防守 / 观察（三选一），并说明原因。

### 2. 💰 组合画像与盈亏归因
（在此处原封不动地插入系统提供的【精准财务数据】表格）
（在表格后结合组合分析师观点，解释仓位集中度、现金比例、资产类别/币种敞口和累计盈亏来源）

### 3. 🛡️ 数据质量与风控边界
- 若 data_quality.status 不是 ok，必须把限制写在最前面。
- 明确指出哪些标的不能给交易建议、哪些建议强度必须下调。

### 4. ⚠️ 最终决断与行动清单
- 必须使用 Markdown 表格，列名固定为：| 动作 | 对象 | 触发条件 | 建议幅度 | 依据 | 主要风险 | 置信度 |。
- 每条建议都必须有触发条件；不得输出无条件加仓、无条件清仓。
- 没有足够证据时，动作必须是"观察"或"核实数据"。
- 建议幅度必须尊重 advice_policy.max_single_trade_weight_pct。

==============================
🚨【系统内部潜规则】（绝对禁止输出以下任何文字到最终报告中）：
1. 财务表格防篡改：在第 2 部分开头插入财务表格时，必须一字不差、原样输出系统提供的数据，严禁修改任何一个字符或排版结构。
2. 列表换行强制要求：当你使用短横线 `- ` 输出列表项，或者输出表格时，在列表或表格的上方，必须强制空出一行（敲击两次回车）。严禁将列表项与上一段文字紧贴！
3. 身份掩饰：绝对不要在报告中提到"根据你的指示"、"系统提示我"或"格式强制红线"等任何暴露你是 AI 或收到过内部指令的话语。
"""
        user_prompt = f"""
【今日核心指数】：{state['indices_data']}
【结构化组合画像 JSON】：\n{_to_prompt_json(state['portfolio_context'])}
【市场上下文 JSON】：\n{_to_prompt_json(state['market_context'])}
【数据质量 JSON】：\n{_to_prompt_json(state['data_quality'])}
【建议策略约束 JSON】：\n{_to_prompt_json(state['advice_policy'])}
【组合分析师观点】：\n{state['portfolio_analysis']}
【市场分析师观点】：\n{state['market_analysis']}
【风控官边界】：\n{state['risk_analysis']}
【精准财务数据 - 持仓明细对账单】：\n{state['portfolio_metrics'].get("markdown_report", "暂无明细数据")}

请生成今日全球盘后报告：
"""
        res = pm_llm.invoke([("system", system_prompt), ("human", user_prompt)])
        return {"final_report": res.content}

    workflow = StateGraph(ReportState)
    
    workflow.add_node("portfolio", portfolio_node)
    workflow.add_node("market", market_node)
    workflow.add_node("risk", risk_node)
    workflow.add_node("pm", pm_node)
    
    workflow.add_edge(START, "portfolio")
    workflow.add_edge("portfolio", "market")
    workflow.add_edge("market", "risk")
    workflow.add_edge("risk", "pm")
    workflow.add_edge("pm", END)
    
    app = workflow.compile()
    
    console.print("\n[bold cyan]🧠 启动日报决策约束引擎 (Portfolio / Market / Risk / PM) ...[/bold cyan]")
    final_state = app.invoke({
        "user_memory": user_memory,
        "indices_data": indices_data,
        "portfolio_metrics": portfolio_metrics,
        "portfolio_context": portfolio_context,
        "market_context": market_context,
        "data_quality": data_quality,
        "advice_policy": advice_policy,
        "portfolio_analysis": "",
        "market_analysis": "",
        "risk_analysis": "",
        "final_report": ""
    })
    
    return final_state["final_report"]


def cleanup_agent_workspace(threshold_mb: int = 500) -> None:
    """
    检查 agent_workspace 容量，超过阈值时按修改时间从旧到新删除文件。

    Args:
        threshold_mb: 触发清理的容量阈值（MB），默认 500MB
    """
    workspace = STOCK_WORKSPACE_DIR
    if not workspace.exists():
        return

    files = [f for f in workspace.iterdir() if f.is_file()]
    total_bytes = sum(f.stat().st_size for f in files)
    total_mb = total_bytes / (1024 * 1024)

    console.print(f"[bold dim]🗂️  [工作区清理] 当前容量：{total_mb:.1f} MB / 阈值：{threshold_mb} MB[/bold dim]")

    if total_mb <= threshold_mb:
        return

    # 按修改时间升序排列（最旧的在前）
    files.sort(key=lambda f: f.stat().st_mtime)
    deleted_count = 0
    deleted_mb = 0.0

    for f in files:
        if total_mb <= threshold_mb * 0.8:  # 清理到阈值的 80% 留出缓冲
            break
        try:
            size_mb = f.stat().st_size / (1024 * 1024)
            f.unlink()
            total_mb -= size_mb
            deleted_mb += size_mb
            deleted_count += 1
            console.print(f"[bold dim]🗑️  已删除：{f.name} ({size_mb:.2f} MB)[/bold dim]")
        except OSError as e:
            console.print(f"[bold red]❌ 删除失败：{f.name} - {e}[/bold red]")

    console.print(f"[bold green]✅ [工作区清理] 共删除 {deleted_count} 个文件，释放 {deleted_mb:.1f} MB[/bold green]")


def job_routine() -> None:
    """
    盘后调度主流程：获取数据 -> 生成报告 -> 发送邮件。
    """
    import multiprocessing
    pid = multiprocessing.current_process().pid
    console.print(f"\n[bold cyan]⏰ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                  f"开始执行盘后调度任务 (PID: {pid})...[/bold cyan]")

    try:
        news_text: str = fetch_global_market_news()
    except RuntimeError as e:
        console.print(f"[bold red]❌ [调度任务] {str(e)}[/bold red]")
        return

    user_memory_dict: Dict[str, Any] = load_user_profile()
    user_memory: str = "\n".join([f"- 【{k}】: {v}" for k, v in user_memory_dict.items()]) if user_memory_dict else "暂无历史持仓与偏好记录"
    console.print(f"[bold dim]🧠 [记忆读取] 用户记忆加载完成[/bold dim]")

    positions = parse_user_profile_to_positions(user_memory_dict)
    cash_assets = parse_cash_assets(user_memory_dict)
    profile_warnings = audit_user_profile_parsing(user_memory_dict, positions, cash_assets)
    valuation = {}
    markdown_report = "暂无持仓数据"
    if positions or cash_assets:
        valuation = calculate_portfolio_valuation(positions, cash_assets)
        markdown_report = format_portfolio_report(valuation)
        console.print(f"[bold dim]💰 [财务计算] 总市值：¥{valuation['total_market_value']:,.2f}, 累计盈亏：¥{valuation['total_profit_loss']:,.2f} ({valuation['profit_loss_percent']:+.2f}%)[/bold dim]")

        # 复用本次估值结果，落盘组合快照供 obs 投资总控台消费（失败不影响日报主流程）
        try:
            from stock_bot.snapshot import write_snapshot
            if write_snapshot(valuation) is not None:
                console.print("[bold dim]📸 [组合快照] 已写入 data/stock/snapshots/portfolio.jsonl[/bold dim]")
        except Exception as e:
            console.print(f"[bold yellow]⚠️ [组合快照] 落盘失败（不影响日报）：{type(e).__name__} - {e}[/bold yellow]")

    portfolio_metrics = {
        "total_market_value": valuation.get("total_market_value", 0.0),
        "total_pnl": valuation.get("total_profit_loss", 0.0),
        "total_pnl_percent": valuation.get("profit_loss_percent", 0.0),
        "markdown_report": markdown_report,
    }

    indices_data: str = fetch_global_indices()
    console.print(f"[bold dim]📊 [指数数据] {indices_data}[/bold dim]")

    portfolio_context = build_portfolio_context(valuation, profile_warnings)
    market_context = build_market_context(news_text, indices_data)
    data_quality = build_data_quality(market_context, portfolio_context)
    advice_policy = build_advice_policy(portfolio_context, data_quality)
    console.print(
        f"[bold dim]🧭 [日报上下文] data_quality={data_quality['status']}, "
        f"风险提示 {len(portfolio_context['risk_flags'])} 条，限制 {len(data_quality['restrictions'])} 条[/bold dim]"
    )

    try:
        report_content: str = generate_market_report(
            user_memory,
            indices_data,
            portfolio_metrics,
            portfolio_context,
            market_context,
            data_quality,
            advice_policy,
        )
    except ValueError as e:
        console.print(f"[bold red]❌ [报告生成] {str(e)}[/bold red]")
        return
    except Exception as e:
        console.print(
            f"[bold red]❌ [报告生成] 大模型推理失败，任务中止：{type(e).__name__}: {e}[/bold red]"
        )
        return

    console.print("[bold green]✔️  [报告生成] 盘后报告生成完毕[/bold green]")

    kb_dir: Path = STOCK_KB_DIR
    kb_dir.mkdir(parents=True, exist_ok=True)

    file_name: str = f"盘后日报_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"

    with open(kb_dir / file_name, "w", encoding="utf-8") as f:
        f.write(report_content)

    console.print(f"[bold green]💾 [知识库归档] 报告快照已成功沉淀至：{file_name}[/bold green]")

    subject: str = f"盘后报告 | {datetime.now().strftime('%Y-%m-%d')}"

    try:
        send_market_report_email(subject, report_content)
        console.print("[bold green]📧 [邮件推送] 报告已成功发送[/bold green]")
    except Exception as e:
        console.print(f"[bold red]❌ [邮件推送] 发送失败：{type(e).__name__} - {str(e)}[/bold red]")

    # 🌟 新增：Telegram 独立推送链路
    try:
        import asyncio
        from core.tg_base import broadcast_to_telegram
        import os as _os
        from pathlib import Path as _Path
        _bot_token = _os.getenv("TG_BOT_TOKEN", "")
        _user_ids = [
            int(u.strip()) for u in _os.getenv("ALLOWED_TG_USERS", "").split(",")
            if u.strip().isdigit()
        ]
        _sandbox = STOCK_WORKSPACE_DIR
        console.print("[bold yellow]🚀 [Telegram 推送] 正在调用渲染引擎下发移动端...[/bold yellow]")

        _result = asyncio.run(broadcast_to_telegram(report_content, _bot_token, _user_ids, _sandbox))
        _ok = _result["success"]
        _fail = _result["failed"]
        if _fail:
            console.print(f"[bold yellow]⚠️  [Telegram 推送] 部分推送失败：成功 {_ok}，失败用户 {_fail}[/bold yellow]")
        else:
            console.print(f"[bold green]📱 [Telegram 推送] 研报已成功推送至全部 {len(_ok)} 位用户！[/bold green]")
    except Exception as e:
        console.print(f"[bold red]❌ [Telegram 推送] 链路崩溃：{e}[/bold red]")

    # 工作区容量巡检与清理
    cleanup_agent_workspace(threshold_mb=500)

    console.print(f"[bold cyan]✅ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 盘后调度任务执行完毕[/bold cyan]\n")


def run_scheduler() -> None:
    """
    启动定时调度器，进入挂起等待状态。
    """
    schedule.every().day.at("16:30").do(job_routine)

    console.print("[bold cyan]🕒 调度器已启动，等待每日 16:30 执行盘后任务...[/bold cyan]")
    console.print("[dim]按 Ctrl+C 停止调度器[/dim]")

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        console.print("\n[bold yellow]⚠️  调度器已停止[/bold yellow]")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ["--test", "-t"]:
        console.print("[bold magenta]🧪 测试模式：立即执行一次盘后调度任务...[/bold magenta]\n")
        job_routine()
    else:
        run_scheduler()

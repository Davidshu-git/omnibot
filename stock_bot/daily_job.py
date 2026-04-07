"""
盘后调度主程序 - 每日盘后热点分析与邮件推送调度器。

本模块负责：
1. 定时调度（每日 15:30 盘后）
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

socket.setdefaulttimeout(30)
from stock_bot.valuation_engine import (
    fetch_stock_price_raw,
    parse_user_profile_to_positions,
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
    news_text: str
    user_memory: str
    indices_data: str
    portfolio_metrics: Dict[str, Any]
    bull_analysis: str
    bear_analysis: str
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
        {"name": "财联社全球电报", "func": _fetch_cls_news},
        {"name": "新浪 7x24", "func": _fetch_sina_news},
        {"name": "东财全球快讯", "func": _fetch_em_news},
    ]

    column_mapping: Dict[str, Dict[str, str]] = {
        "财联社全球电报": {"time": "发布时间", "content": "内容"},
        "新浪 7x24": {"time": "时间", "content": "内容"},
        "东财全球快讯": {"time": "发布时间", "content": "摘要"},
    }

    dfs = []

    for source in data_sources:
        source_name: str = source["name"]
        fetch_func = source["func"]

        try:
            df: pd.DataFrame = fetch_func()

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
        except Exception as e:
            console.print(f"[bold red]❌ {source_name} 获取失败：{type(e).__name__} - {str(e)}[/bold red]")
            continue

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


def generate_market_report(news_text: str, user_memory: str, indices_data: str, portfolio_metrics: Dict[str, Any]) -> str:
    """基于 LangGraph 的多智能体研报辩论引擎"""
    dashscope_key: str = os.getenv("ALI_CODING_PLAN_KEY", "")
    if not dashscope_key:
        raise ValueError("ALI_CODING_PLAN_KEY 未配置")

    llm = ChatOpenAI(
        model="qwen3.5-plus",
        base_url="https://coding.dashscope.aliyuncs.com/v1",
        temperature=0.7,  # 牛熊辩论节点：激进发散，挖掘极端多空逻辑
        timeout=120,
        max_retries=3,
        max_tokens=8192,
        api_key=SecretStr(dashscope_key),
    )

    pm_llm = ChatOpenAI(
        model="qwen3.5-plus",
        base_url="https://coding.dashscope.aliyuncs.com/v1",
        temperature=0.1,  # PM 裁判节点：保守收敛，确保最终结论严谨客观
        timeout=120,
        max_retries=3,
        max_tokens=8192,
        api_key=SecretStr(dashscope_key),
    )

    def bull_node(state: ReportState):
        console.print("[bold green]🐂 [Agent 1] 激进策略师正在挖掘利好与翻倍逻辑...[/bold green]")
        prompt = ChatPromptTemplate.from_template(
            "你是一位极度乐观的激进策略师（The Bull）。你的任务是从以下信息中死命挖掘利好、技术突破、翻倍逻辑和政策支持。忽略一切风险因素！\n"
            "【今日指数】：{indices_data}\n"
            "【全球资讯】：{news_text}\n"
            "【用户持仓】：{user_memory}\n"
            "请输出一份针对该用户持仓的【激进看多分析】（限 400 字以内，语气要充满激情、带点华尔街狼性的煽动性）。"
        )
        chain = prompt | llm
        res = chain.invoke(state)
        return {"bull_analysis": res.content}

    def bear_node(state: ReportState):
        console.print("[bold red]🐻 [Agent 2] 首席风控官正在嗅探黑天鹅与危机...[/bold red]")
        prompt = ChatPromptTemplate.from_template(
            "你是一位极度悲观、甚至有被迫害妄想症的首席风控官（The Bear）。你的任务是从以下信息中专门挖掘地缘政治、泡沫、供应链断裂等一切可能导致用户亏钱的隐患。对利好视而不见！\n"
            "【今日指数】：{indices_data}\n"
            "【全球资讯】：{news_text}\n"
            "【用户持仓】：{user_memory}\n"
            "请输出一份针对该用户持仓的【极度看空与风险警告】（限 400 字以内，语气要极其严厉、警惕）。"
        )
        chain = prompt | llm
        res = chain.invoke(state)
        return {"bear_analysis": res.content}

    def pm_node(state: ReportState):
        console.print("[bold magenta]👨‍⚖️ [Agent 3] 投资总监正在进行多空对决裁决与最终排版...[/bold magenta]")
        system_prompt = """你是一位顶级的华尔街投资总监（PM）。你需要审视激进策略师（Bull）和首席风控官（Bear）的辩论，结合用户的【精准财务明细】，输出最终的盘后研报。

你的回复必须严格采用 Markdown 格式，并强制包含以下三大核心模块：

### 1. 🌍 宏观与多空博弈复盘
- 综合今日核心指数表现。
- 提炼 Bull 和 Bear 的核心观点冲突，并给出你作为投资总监的最终客观评判（当前市场是该贪婪还是该恐惧？）。

### 2. 💰 专属市值与盈亏归因分析
（在此处原封不动地插入系统提供的【精准财务数据】表格）
（在此处结合多空双方的观点，对用户的【累计盈亏】进行深度归因分析）

### 3. ⚠️ 最终决断与调仓建议
- 明确指出当前持仓最大的风险敞口在哪里。
- 给出明确的、可操作的调仓建议（如：保持观望、降低某赛道仓位、逢低建仓等）。

==============================
🚨【系统内部潜规则】（绝对禁止输出以下任何文字到最终报告中）：
1. 财务表格防篡改：在第 2 部分开头插入财务表格时，必须一字不差、原样输出系统提供的数据，严禁修改任何一个字符或排版结构。
2. 列表换行强制要求：当你使用短横线 `- ` 输出列表项，或者输出表格时，在列表或表格的上方，必须强制空出一行（敲击两次回车）。严禁将列表项与上一段文字紧贴！
3. 身份掩饰：绝对不要在报告中提到"根据你的指示"、"系统提示我"或"格式强制红线"等任何暴露你是 AI 或收到过内部指令的话语。
"""
        user_prompt = f"""
【今日核心指数】：{state['indices_data']}
【激进策略师观点】：\n{state['bull_analysis']}
【首席风控官观点】：\n{state['bear_analysis']}
【精准财务数据 - 持仓明细对账单】：\n{state['portfolio_metrics'].get("markdown_report", "暂无明细数据")}

请生成今日全球盘后报告：
"""
        res = pm_llm.invoke([("system", system_prompt), ("human", user_prompt)])
        return {"final_report": res.content}

    workflow = StateGraph(ReportState)
    
    workflow.add_node("bull", bull_node)
    workflow.add_node("bear", bear_node)
    workflow.add_node("pm", pm_node)
    
    workflow.add_edge(START, "bull")
    workflow.add_edge("bull", "bear")
    workflow.add_edge("bear", "pm")
    workflow.add_edge("pm", END)
    
    app = workflow.compile()
    
    console.print("\n[bold cyan]🧠 启动华尔街虚拟交易室 (Multi-Agent Debate) ...[/bold cyan]")
    final_state = app.invoke({
        "news_text": news_text,
        "user_memory": user_memory,
        "indices_data": indices_data,
        "portfolio_metrics": portfolio_metrics,
        "bull_analysis": "",
        "bear_analysis": "",
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
    valuation = {}
    markdown_report = "暂无持仓数据"
    if positions:
        valuation = calculate_portfolio_valuation(positions)
        markdown_report = format_portfolio_report(valuation)
        console.print(f"[bold dim]💰 [财务计算] 总市值：¥{valuation['total_market_value']:,.2f}, 累计盈亏：¥{valuation['total_profit_loss']:,.2f} ({valuation['profit_loss_percent']:+.2f}%)[/bold dim]")

    portfolio_metrics = {
        "total_market_value": valuation.get("total_market_value", 0.0),
        "total_pnl": valuation.get("total_profit_loss", 0.0),
        "total_pnl_percent": valuation.get("profit_loss_percent", 0.0),
        "markdown_report": markdown_report,
    }

    indices_data: str = fetch_global_indices()
    console.print(f"[bold dim]📊 [指数数据] {indices_data}[/bold dim]")

    try:
        report_content: str = generate_market_report(news_text, user_memory, indices_data, portfolio_metrics)
    except ValueError as e:
        console.print(f"[bold red]❌ [报告生成] {str(e)}[/bold red]")
        return
    except Exception:
        console.print("[bold red]❌ [报告生成] 大模型推理失败，任务中止。[/bold red]")
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
    schedule.every().day.at("15:30").do(job_routine)

    console.print("[bold cyan]🕒 调度器已启动，等待每日 15:30 执行盘后任务...[/bold cyan]")
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

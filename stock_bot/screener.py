"""
选股筛股引擎——按"顺大势"方法论批量筛选股票池。

只做"仪表盘式"多维度展示，不产出单一黑箱评分：展示可拆解、可核实的信号列
（MA250 方向硬性过滤 + 相对强度 + 趋势持续天数 + 偏离度历史分位），排序不代替
用户下判断，也绝不产出买卖建议——延续 valuation_engine.fetch_stock_trend 的
免责基调。加密货币不纳入筛选（大盘/行业联动等概念对 crypto 没有对应意义）。
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yfinance as yf

from stock_bot.valuation_engine import (
    _MA_TREND_LOOKBACK,
    _ma_trend_info,
    detect_ticker_currency,
    format_universal_ticker,
    is_crypto_ticker,
)

logger = logging.getLogger(__name__)

# 按标的原生货币映射基准指数——与 daily_job.py::fetch_global_indices 用的同一批
# yfinance 代码（已在生产验证过），避免另起一套未经验证的符号。
_BENCHMARK_BY_CURRENCY: Dict[str, str] = {"USD": "^GSPC", "HKD": "^HSI", "CNY": "000300.SS"}

_SCAN_WORKERS = 15
_RELATIVE_STRENGTH_WINDOW = 60  # 近 N 个交易日收益率，用于跟基准比强弱
_SCAN_PERIOD = "2y"  # 与 fetch_stock_trend 同款窗口，兼顾 MA250 计算与批量扫描耗时

# 预置美股股票池：随代码库打包的静态资源（非 data/ 下的运行时用户数据，随 git 版本控制），
# 源自 nasdaqtrader.com 官方代码目录，已过滤 ETF/权证/权利/单位/优先股/SPAC 等非普通股。
_PRESET_US_STOCKS_PATH = Path(__file__).parent / "screener_presets" / "us_common_stocks.json"


def load_preset_us_stocks() -> List[str]:
    """读取随代码库打包的预置美股股票池（"一键载入"按钮用）。

    Returns:
        List[str]: 代码列表；预置文件缺失或损坏返回空列表（不抛异常，前端优雅降级）。
    """
    if not _PRESET_US_STOCKS_PATH.exists():
        return []
    try:
        data = json.loads(_PRESET_US_STOCKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("[screener] 读取预置股票池失败：%s", _PRESET_US_STOCKS_PATH)
        return []
    tickers = data.get("tickers") if isinstance(data, dict) else None
    return tickers if isinstance(tickers, list) else []


def _universe_path(memory_dir: Path) -> Path:
    return memory_dir / "screener" / "universe.json"


def _status_path(memory_dir: Path) -> Path:
    return memory_dir / "screener" / "scan_status.json"


def load_universe(memory_dir: Path) -> List[Dict[str, Any]]:
    """读取股票池，文件不存在或损坏返回空列表（不抛异常，供页面首次使用时优雅降级）。"""
    path = _universe_path(memory_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("[screener] 读取股票池失败：%s", path)
        return []
    return data if isinstance(data, list) else []


def save_universe(memory_dir: Path, tickers: List[str]) -> List[Dict[str, Any]]:
    """整体覆盖保存股票池（obs 页面文本框按行拆分后整体提交，非增量追加）。

    Args:
        memory_dir: 记忆目录（`data/stock/memory`）。
        tickers: 原始代码列表，空白项与首尾空白会被清理。

    Returns:
        List[Dict[str, Any]]: 保存后的股票池（`[{"ticker": ...}]`），供前端回显。
    """
    path = _universe_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    universe = [{"ticker": t.strip()} for t in tickers if t.strip()]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(universe, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return universe


def _write_status(memory_dir: Path, status: Dict[str, Any]) -> None:
    path = _status_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_status(memory_dir: Path) -> Dict[str, Any]:
    """读取扫描状态，文件不存在或损坏视为 idle（从未扫描过）。"""
    path = _status_path(memory_dir)
    if not path.exists():
        return {"status": "idle"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "idle"}


def _fetch_benchmark_returns(currencies: List[str]) -> Dict[str, Optional[float]]:
    """按用到的币种各拉一次基准指数近 N 交易日收益率，一次扫描内每个基准只拉一次。

    单一基准取数失败只让该币种下所有标的的相对强度列标 None（不可用），不影响
    其余市场的筛选结果与硬性过滤——不能因为一个指数没拉到就拖垮整次扫描。
    """
    returns: Dict[str, Optional[float]] = {}
    for cur in currencies:
        benchmark = _BENCHMARK_BY_CURRENCY.get(cur)
        if not benchmark:
            continue
        try:
            hist = yf.Ticker(benchmark).history(period=_SCAN_PERIOD)
            close = hist["Close"].dropna()
            if len(close) <= _RELATIVE_STRENGTH_WINDOW:
                returns[cur] = None
                continue
            returns[cur] = float(
                (close.iloc[-1] / close.iloc[-_RELATIVE_STRENGTH_WINDOW - 1] - 1) * 100
            )
        except Exception:
            logger.warning("[screener] 基准指数 %s 取数失败", benchmark)
            returns[cur] = None
    return returns


def _screen_one(ticker: str, benchmark_returns: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """单只标的的筛选逻辑：MA250 方向硬性过滤 + 相对强度/趋势持续天数/偏离度分位。

    Returns:
        Dict[str, Any]: ``passed=False`` 表示被过滤或取数失败（``skip_reason`` 说明
        原因），``passed=True`` 时附完整信号字段。
    """
    if is_crypto_ticker(ticker):
        return {"ticker": ticker, "passed": False, "skip_reason": "不支持加密货币"}

    try:
        formatted = format_universal_ticker(ticker)
        hist = yf.Ticker(formatted).history(period=_SCAN_PERIOD)
        if hist is None or hist.empty:
            return {"ticker": formatted, "passed": False, "skip_reason": "无历史数据"}

        close = hist["Close"].dropna()
        ma60 = close.rolling(60).mean()
        ma250 = close.rolling(250).mean()
        latest_price = float(close.iloc[-1])

        ma250_info = _ma_trend_info(ma250, close, latest_price, _MA_TREND_LOOKBACK["ma250"])
        if not ma250_info.get("available"):
            return {"ticker": formatted, "passed": False, "skip_reason": "历史数据不足（算不出年线）"}
        if ma250_info["direction"] != "向上":
            return {"ticker": formatted, "passed": False, "skip_reason": f"年线{ma250_info['direction']}，不符合硬性过滤"}

        ma60_info = _ma_trend_info(ma60, close, latest_price, _MA_TREND_LOOKBACK["ma60"])

        currency = detect_ticker_currency(formatted)
        bench_return = benchmark_returns.get(currency)
        relative_strength: Optional[float] = None
        if bench_return is not None and len(close) > _RELATIVE_STRENGTH_WINDOW:
            stock_return = float(
                (close.iloc[-1] / close.iloc[-_RELATIVE_STRENGTH_WINDOW - 1] - 1) * 100
            )
            relative_strength = round(stock_return - bench_return, 2)

        # 趋势持续天数：MA250 逐日一阶差分连续为正的交易日数，从最新一天往回数；
        # 数到取数窗口起点仍未转负，标 capped=True——诚实说明"至少这么久"，不假装
        # 知道 2 年取数窗口以外的真实时长。
        deltas = ma250.dropna().diff().dropna()
        duration = 0
        capped = True
        for v in deltas.iloc[::-1]:
            if v <= 0:
                capped = False
                break
            duration += 1

        return {
            "ticker": formatted,
            "passed": True,
            "latest_price": round(latest_price, 4),
            "ma250_direction": ma250_info["direction"],
            "relative_strength_pct": relative_strength,
            "trend_duration_days": duration,
            "trend_duration_capped": capped,
            "deviation_percentile_ma60": (
                ma60_info.get("deviation_percentile") if ma60_info.get("available") else None
            ),
        }
    except Exception as exc:
        logger.warning("[screener] 扫描 %s 失败：%s", ticker, exc)
        return {"ticker": ticker, "passed": False, "skip_reason": f"取数异常：{type(exc).__name__}"}


def screen_universe(
    universe: List[Dict[str, Any]],
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """并发批量筛选整份股票池。

    Args:
        universe: `load_universe()` 返回的列表，每项至少含 `ticker`（可选 `tag`，
            用户自己标注的"大盘/龙头"等备注，不参与算法打分，原样回传）。
        progress_cb: 每完成一只标的调用一次 `(done, total)`，供进度上报；
            在 `ThreadPoolExecutor` 的调用线程里执行，需自行保证线程安全。

    Returns:
        Dict[str, Any]: `{results, skipped, total, passed_count}`。`results` 按
        `relative_strength_pct` 降序（None 排最后）。
    """
    total = len(universe)
    tags = {u["ticker"]: u.get("tag") for u in universe if u.get("ticker")}
    tickers = list(tags.keys())

    currencies_needed = set()
    for t in tickers:
        if is_crypto_ticker(t):
            continue
        try:
            currencies_needed.add(detect_ticker_currency(format_universal_ticker(t)))
        except Exception:
            continue
    benchmark_returns = _fetch_benchmark_returns(list(currencies_needed))

    results: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as executor:
        futures = {executor.submit(_screen_one, t, benchmark_returns): t for t in tickers}
        for future in as_completed(futures):
            item = future.result()
            item["tag"] = tags.get(futures[future])
            if item.get("passed"):
                results.append(item)
            else:
                skipped.append(item)
            done += 1
            if progress_cb:
                progress_cb(done, total)

    results.sort(key=lambda r: (r["relative_strength_pct"] is None, -(r["relative_strength_pct"] or 0)))

    return {"results": results, "skipped": skipped, "total": total, "passed_count": len(results)}


def run_scan_and_write_status(memory_dir: Path) -> None:
    """扫描 + 全程写状态文件（obs 轮询读这个文件）。同步阻塞，调用方需丢线程池跑。

    Args:
        memory_dir: 记忆目录（`data/stock/memory`），股票池与状态文件均落在其下的
            `screener/` 子目录。
    """
    universe = load_universe(memory_dir)
    total = len(universe)
    started_at = datetime.utcnow().isoformat() + "Z"
    _write_status(memory_dir, {"status": "running", "total": total, "done": 0, "started_at": started_at})

    def progress_cb(done: int, tot: int) -> None:
        _write_status(memory_dir, {"status": "running", "total": tot, "done": done, "started_at": started_at})

    try:
        outcome = screen_universe(universe, progress_cb=progress_cb)
    except Exception as exc:
        logger.exception("[screener] 扫描失败")
        _write_status(memory_dir, {
            "status": "error", "total": total, "done": 0,
            "started_at": started_at, "error": str(exc) or type(exc).__name__,
        })
        return

    _write_status(memory_dir, {
        "status": "done",
        "total": total,
        "done": total,
        "started_at": started_at,
        "completed_at": datetime.utcnow().isoformat() + "Z",
        **outcome,
    })

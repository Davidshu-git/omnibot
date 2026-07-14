"""
选股筛股引擎——按"顺大势"方法论批量筛选股票池。

只做"仪表盘式"多维度展示，不产出单一黑箱评分：展示可拆解、可核实的信号列
（MA250 方向硬性过滤 + 相对强度 + 逆市抗跌强度 + 真实趋势年限 + 偏离度历史分位
+ 逆小势回调观察），排序不代替用户下判断，也绝不产出买卖建议——延续
valuation_engine.fetch_stock_trend 的免责基调。加密货币不纳入筛选（大盘/行业
联动等概念对 crypto 没有对应意义）。

「势能」加分维度（呼应"顺势而为，势=强度"）：① 逆市抗跌强度——只在大盘下跌日
衡量个股日超额收益均值，抗跌/逆势才是难被操纵的真信号；② 真实趋势年限——取数
窗口 6 年，MA250 连涨可数到 ~5 年，区分长寿趋势 vs 刚涨两三年。「大盘股优先」由
精选池（标普 500 + 恒生科技，皆大盘股）天然覆盖，不再逐只拉 .info 市值；「板块/
产业链龙头联动」为定性判断、无法可靠自动化，留给用户手动做基本面。
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
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
# 取数窗口 6 年：为「长寿趋势」——趋势持续天数原被 2y 窗口截断（涨 5 年和涨 2 年都
# 显示 500+ 日，分不出来），拉长到 6y 后 MA250 上升可连续数到 ~5 年，真正区分长寿趋势
# vs 刚涨两三年。代价是每只取数更重、批量扫描更慢。
_SCAN_PERIOD = "6y"
_TRADING_DAYS_PER_YEAR = 250  # 交易日→年的换算（趋势持续天数→真实趋势年限）

# 「逆小势」回调观察阈值：在「顺大势」（年线向上，硬过滤已保证）前提下，若现价已跌破
# 中期线 MA60、但仍在年线 MA250 上方（回调而非趋势破位），且 MA60 偏离度处于该标的自身
# 历史低位分位（恐慌情绪钟摆摆到底），标记为「回调观察」。仅缩小盯盘范围、绝不构成买入建议
# ——延续本模块「只做仪表盘、不产出买卖建议」的免责基调（接飞刀风险仍在，年线可能后续破位）。
_PULLBACK_PANIC_PERCENTILE = 20.0

# 预置精选池：随代码库打包的静态资源（非 data/ 下运行时用户数据，随 git 版本控制），
# = 标普 500 + 恒生科技指数 成分股（构建期由 screener_presets/gen_preset_pool.py 离线
# 生成，「静态快照 + 手动定期重生成」）。收窄自旧「4875 只全市场美股」，大幅缩小检索范围
# 并补齐港股空白。
_PRESET_POOL_PATH = Path(__file__).parent / "screener_presets" / "preset_pool.json"

# 代码→常用名 静态映射：构建期由 screener_presets/gen_ticker_names.py 离线生成，
# 运行时纯查表补名（美股英文名 / A 股中文名 / 港股名），零网络成本、零限流风险。
_TICKER_NAMES_PATH = Path(__file__).parent / "screener_presets" / "ticker_names.json"


@lru_cache(maxsize=1)
def _load_ticker_names() -> Dict[str, str]:
    """加载「格式化代码→常用名」映射（进程内缓存一次）。

    文件缺失或损坏返回空 dict（前端回退显示代码，永不阻塞筛选）。
    """
    if not _TICKER_NAMES_PATH.exists():
        return {}
    try:
        data = json.loads(_TICKER_NAMES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("[screener] 读取常用名映射失败：%s", _TICKER_NAMES_PATH)
        return {}
    names = data.get("names") if isinstance(data, dict) else None
    return names if isinstance(names, dict) else {}


def _name_of(formatted_ticker: str) -> Optional[str]:
    """查单只常用名，未收录返回 None（前端显示 ``—``）。"""
    return _load_ticker_names().get(formatted_ticker)


def resolve_ticker_name(raw_ticker: str) -> Optional[str]:
    """把用户原始代码归一化后查常用名（观察清单等展示场景复用），查不到/异常返回 None。

    Args:
        raw_ticker: 用户原始输入（如 ``AAPL`` / ``0700`` / ``600519`` / ``BTC``）。

    Returns:
        Optional[str]: 常用名；未收录（含加密货币等未进静态映射的）返回 None。
    """
    try:
        return _name_of(format_universal_ticker(raw_ticker.strip()))
    except Exception:  # noqa: BLE001 —— 取名是纯展示增强，任何异常都降级为无名
        return None


def load_preset_pool() -> List[str]:
    """读取随代码库打包的预置精选池（标普 500 + 恒生科技，"一键载入"按钮用）。

    Returns:
        List[str]: 代码列表；预置文件缺失或损坏返回空列表（不抛异常，前端优雅降级）。
    """
    if not _PRESET_POOL_PATH.exists():
        return []
    try:
        data = json.loads(_PRESET_POOL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("[screener] 读取预置股票池失败：%s", _PRESET_POOL_PATH)
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


def _fetch_benchmark_data(currencies: List[str]) -> Dict[str, Dict[str, Any]]:
    """按用到的币种各拉一次基准指数，一次扫描内每个基准只拉一次。

    每个币种返回 ``{"return": 近 N 交易日收益率 or None, "daily": 日收益率序列 or None}``：
      - ``return`` 供「相对强度」（笼统跑赢/跑输幅度）。
      - ``daily`` 供「逆市强度」（只在大盘下跌日衡量个股抗跌能力）。

    单一基准取数失败只让该币种下所有标的的相关列标 None（不可用），不影响其余市场的
    筛选结果与硬性过滤——不能因为一个指数没拉到就拖垮整次扫描。
    """
    data: Dict[str, Dict[str, Any]] = {}
    for cur in currencies:
        benchmark = _BENCHMARK_BY_CURRENCY.get(cur)
        if not benchmark:
            continue
        try:
            hist = yf.Ticker(benchmark).history(period=_SCAN_PERIOD)
            close = hist["Close"].dropna()
            if len(close) <= _RELATIVE_STRENGTH_WINDOW:
                data[cur] = {"return": None, "daily": None}
                continue
            data[cur] = {
                "return": float(
                    (close.iloc[-1] / close.iloc[-_RELATIVE_STRENGTH_WINDOW - 1] - 1) * 100
                ),
                "daily": close.pct_change().dropna(),
            }
        except Exception:
            logger.warning("[screener] 基准指数 %s 取数失败", benchmark)
            data[cur] = {"return": None, "daily": None}
    return data


def _screen_one(ticker: str, benchmark_data: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """单只标的的筛选逻辑：MA250 方向硬性过滤 + 相对强度/逆市强度/趋势持续天数/偏离度分位。

    Returns:
        Dict[str, Any]: ``passed=False`` 表示被过滤或取数失败（``skip_reason`` 说明
        原因），``passed=True`` 时附完整信号字段。
    """
    if is_crypto_ticker(ticker):
        return {"ticker": ticker, "name": _name_of(ticker), "passed": False, "skip_reason": "不支持加密货币"}

    try:
        formatted = format_universal_ticker(ticker)
        name = _name_of(formatted)
        hist = yf.Ticker(formatted).history(period=_SCAN_PERIOD)
        if hist is None or hist.empty:
            return {"ticker": formatted, "name": name, "passed": False, "skip_reason": "无历史数据"}

        close = hist["Close"].dropna()
        ma60 = close.rolling(60).mean()
        ma250 = close.rolling(250).mean()
        latest_price = float(close.iloc[-1])

        ma250_info = _ma_trend_info(ma250, close, latest_price, _MA_TREND_LOOKBACK["ma250"])
        if not ma250_info.get("available"):
            return {"ticker": formatted, "name": name, "passed": False, "skip_reason": "历史数据不足（算不出年线）"}
        if ma250_info["direction"] != "向上":
            return {"ticker": formatted, "name": name, "passed": False, "skip_reason": f"年线{ma250_info['direction']}，不符合硬性过滤"}

        ma60_info = _ma_trend_info(ma60, close, latest_price, _MA_TREND_LOOKBACK["ma60"])

        currency = detect_ticker_currency(formatted)
        bench = benchmark_data.get(currency) or {}
        bench_return = bench.get("return")
        bench_daily = bench.get("daily")

        relative_strength: Optional[float] = None
        if bench_return is not None and len(close) > _RELATIVE_STRENGTH_WINDOW:
            stock_return = float(
                (close.iloc[-1] / close.iloc[-_RELATIVE_STRENGTH_WINDOW - 1] - 1) * 100
            )
            relative_strength = round(stock_return - bench_return, 2)

        # 逆市抗跌强度：只在「基准下跌日」衡量个股日超额收益的均值——大盘跌的时候还能
        # 抗跌甚至逆势上涨，才是「势能强、难被操纵」的真信号（区别于笼统相对强度）。
        # 为正=下跌市里跑赢大盘（抗跌/逆势），越大越抗跌；样本对齐取交易日交集。
        counter_trend_strength: Optional[float] = None
        if bench_daily is not None:
            stock_daily = close.pct_change().dropna().iloc[-_RELATIVE_STRENGTH_WINDOW:]
            aligned = pd.concat([stock_daily, bench_daily], axis=1, join="inner").dropna()
            if not aligned.empty:
                aligned.columns = ["s", "b"]
                down = aligned[aligned["b"] < 0]
                if not down.empty:
                    counter_trend_strength = round(float((down["s"] - down["b"]).mean()) * 100, 3)

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

        # 「逆小势」回调观察：顺大势（年线向上）+ 跌破 MA60 + 仍在年线上方 + MA60 偏离度
        # 处历史低位（恐慌钟摆）。四条件全真才标记，任一字段缺失即视为 False（不误报）。
        dev_pct_ma60 = ma60_info.get("deviation_pct") if ma60_info.get("available") else None
        dev_pctile_ma60 = ma60_info.get("deviation_percentile") if ma60_info.get("available") else None
        pullback_watch = (
            dev_pct_ma60 is not None
            and dev_pct_ma60 < 0
            and ma250_info["deviation_pct"] > 0
            and dev_pctile_ma60 is not None
            and dev_pctile_ma60 <= _PULLBACK_PANIC_PERCENTILE
        )

        return {
            "ticker": formatted,
            "name": name,
            "passed": True,
            "latest_price": round(latest_price, 4),
            "ma250_direction": ma250_info["direction"],
            "relative_strength_pct": relative_strength,
            "counter_trend_strength": counter_trend_strength,
            "trend_duration_days": duration,
            "trend_duration_years": round(duration / _TRADING_DAYS_PER_YEAR, 1),
            "trend_duration_capped": capped,
            "deviation_percentile_ma60": dev_pctile_ma60,
            "pullback_watch": bool(pullback_watch),
        }
    except Exception as exc:
        logger.warning("[screener] 扫描 %s 失败：%s", ticker, exc)
        return {"ticker": ticker, "name": _name_of(ticker), "passed": False, "skip_reason": f"取数异常：{type(exc).__name__}"}


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
    benchmark_data = _fetch_benchmark_data(list(currencies_needed))

    results: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as executor:
        futures = {executor.submit(_screen_one, t, benchmark_data): t for t in tickers}
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

"""构建期一次性生成选股「精选预置池」（``preset_pool.json``）。

预置池收窄自「4875 只全市场美股」为 **标普 500 + 恒生科技指数** 两大成分股集合——
大幅缩小需批量分析检索的范围，且都是流动性好、yfinance 数据齐的大盘股，取数失败率
低，同时补齐了旧池完全没有的港股。遵循「预置资源随 git 打包、运行时零网络」范式；
这里是「静态快照 + 手动定期重生成」，不实时。

数据源：
  - 标普 500：datahub ``s-and-p-500-companies`` 官方镜像 CSV（github raw，稳定、
    带版本；``Symbol`` 列，点号类别码 ``BRK.B``→yfinance 的 ``BRK-B``）。
  - 恒生科技：eastmoney push2 指数成分接口（``fs=i:124.HSTECH``，带重试）；该端点
    在本机代理链路时有瞬断，拉不到时回退到脚本内**已核实的成分快照**（附上次核对
    日期，需人工定期更新——恒科每季度调仓）。

代码统一走 ``format_universal_ticker``：标普 500 输出裸字母码（``AAPL``/``BRK-B``），
恒科输出 ``NNNN.HK``（如 ``0700.HK``），与 ``screener._screen_one`` 的 ticker 对齐。

用法（在有网络、装了 stock_bot 依赖的 stock-bot 容器内跑）：
    docker exec v2-omnistock-tg-bot python -m stock_bot.screener_presets.gen_preset_pool
"""

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_OUT_PATH = Path(__file__).parent / "preset_pool.json"

_SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)

# 恒生科技指数成分：eastmoney push2 指数成分端点（secid 124.HSTECH）。
_HSTECH_CONS_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn=1&pz=100&fltt=2&fields=f12,f13,f14&fs=i:124.HSTECH"
)
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}

# 恒科成分**已核实快照**（AASTOCKS live，末次核对 2026-07-14，恒指公司 2026-05 调整后
# 的 30 只）。仅当 eastmoney live 端点拉取失败时兜底使用——恒科每季度调仓，重生成时
# 若走了兜底，请对照 https://www.aastocks.com/en/stocks/market/index/hk-index-con.aspx?index=HSTECH
# 手动核对更新此列表。存 5 位原始码，交给 format_universal_ticker 归一化。
_HSTECH_SNAPSHOT_CODES: List[str] = [
    "00020", "00100", "00241", "00285", "00300", "00700", "00780", "00981",
    "00992", "01024", "01211", "01347", "01698", "01810", "02015", "02382",
    "02513", "03690", "06618", "06690", "09618", "09626", "09660", "09863",
    "09866", "09868", "09888", "09961", "09988", "09999",
]
_HSTECH_SNAPSHOT_ASOF = "2026-07-14"


def _fetch_sp500(fmt) -> List[str]:
    """拉标普 500 成分代码（datahub 官方镜像 CSV），点号类别码转 yfinance 连字符形式。

    Args:
        fmt: ``format_universal_ticker``，对美股裸字母码原样返回、统一大小写。

    Returns:
        List[str]: 格式化后的美股代码；下载或解析失败返回空列表（调用方决定是否致命）。
    """
    import csv
    import io

    try:
        raw = urllib.request.urlopen(
            urllib.request.Request(_SP500_CSV_URL, headers=_HTTP_HEADERS), timeout=30
        ).read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 —— 构建脚本，失败降级即可
        logger.warning("[gen_pool] 标普 500 源下载失败：%s", exc)
        return []

    tickers: List[str] = []
    for rec in csv.DictReader(io.StringIO(raw)):
        sym = (rec.get("Symbol") or "").strip().upper()
        if not sym:
            continue
        # yfinance 用连字符表达股份类别（BRK.B→BRK-B / BF.B→BF-B），CSV 用点号。
        tickers.append(fmt(sym.replace(".", "-")))
    logger.info("[gen_pool] 标普 500：%d 只", len(tickers))
    return tickers


def _fetch_hstech_live(fmt) -> List[str]:
    """尝试从 eastmoney 指数成分端点拉恒科成分（带重试）；失败返回空列表。"""
    for attempt in range(5):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(_HSTECH_CONS_URL, headers=_HTTP_HEADERS), timeout=20
            ).read().decode("utf-8")
            data = (json.loads(raw) or {}).get("data") or {}
            diff = data.get("diff")
            rows = list(diff.values()) if isinstance(diff, dict) else (diff or [])
            codes = [str(r.get("f12", "")).strip() for r in rows if r.get("f12")]
            if codes:
                logger.info("[gen_pool] 恒科 live：%d 只", len(codes))
                return [fmt(c) for c in codes]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[gen_pool] 恒科 live 第 %d 次失败：%s", attempt + 1, type(exc).__name__)
        time.sleep(2)
    return []


def _fetch_hstech(fmt) -> List[str]:
    """恒科成分：优先 eastmoney live，失败回退到脚本内已核实快照。"""
    codes = _fetch_hstech_live(fmt)
    if codes:
        return codes
    logger.warning(
        "[gen_pool] 恒科 live 全部失败，回退快照（核对于 %s，共 %d 只）",
        _HSTECH_SNAPSHOT_ASOF, len(_HSTECH_SNAPSHOT_CODES),
    )
    return [fmt(c) for c in _HSTECH_SNAPSHOT_CODES]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from stock_bot.valuation_engine import format_universal_ticker

    def fmt(code: str) -> str:
        return format_universal_ticker(code)

    sp500 = _fetch_sp500(fmt)
    if not sp500:
        raise SystemExit("标普 500 源拉取失败，终止（不生成半份池子覆盖旧文件）")
    hstech = _fetch_hstech(fmt)

    # 去重保序：标普在前、恒科在后（同一只理论上不会跨市场重复，防御性去重）。
    seen: set = set()
    tickers: List[str] = []
    for t in sp500 + hstech:
        if t and t not in seen:
            seen.add(t)
            tickers.append(t)

    payload = {
        "source": "S&P 500 (datahub constituents.csv) + Hang Seng TECH (eastmoney i:124.HSTECH, snapshot fallback)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(tickers),
        "sp500_count": len(sp500),
        "hstech_count": len(hstech),
        "tickers": tickers,
    }
    tmp = _OUT_PATH.with_suffix(_OUT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_OUT_PATH)
    logger.info(
        "[gen_pool] 已写入 %s（标普 %d + 恒科 %d = 合计 %d）",
        _OUT_PATH, len(sp500), len(hstech), len(tickers),
    )


if __name__ == "__main__":
    main()

"""构建期一次性生成 ticker→常用名 静态映射（``ticker_names.json``）。

选股结果只显示代码对人不友好，此脚本预先把「代码→常用名」离线固化成随 git 版本控制
的静态资源，运行时（``screener.py``）纯查表、零网络成本、零限流风险——延续
``us_common_stocks.json`` 的「预置资源打包」范式。

数据源（各市场用最稳的免费源）：
  - 美股：nasdaqtrader.com 官方代码目录（``nasdaqlisted``/``otherlisted``，自带
    Security Name，一次下载覆盖全市场）。
  - A 股：akshare ``stock_info_a_code_name``（一次调用返回全部 code→name）。
  - 港股：akshare ``stock_hk_famous_spot_em``/``stock_hk_ggt_components_em``（eastmoney
    港股端点在部分网络下不稳，带重试；仍拿不到的由 yfinance 兜底）。
  - 兜底：对「用户当前股票池」里任何仍缺名的代码，用 yfinance ``.info`` 逐只补齐
    （构建期跑，慢/限流可接受，只补少量缺口）。

键统一用 ``format_universal_ticker`` 的产物（如 ``AAPL``/``600519.SS``/``0700.HK``），
与 ``screener._screen_one`` 返回的 ``ticker`` 对齐，运行时可直接 ``dict.get`` 命中。

用法（必须在装有 akshare + 有网络的 stock-bot 容器内跑）：
    docker exec v2-omnistock-tg-bot python -m stock_bot.screener_presets.gen_ticker_names
"""

import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_OUT_PATH = Path(__file__).parent / "ticker_names.json"
_UNIVERSE_PATH = Path(__file__).resolve().parents[2] / "data" / "stock" / "memory" / "screener" / "universe.json"

_NASDAQ_FILES = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)

# 美股 Security Name 常带的股份类别尾巴，展示「常用名」时去掉更干净。nasdaqtrader
# 两种写法都有：带分隔号（"Foo - Common Stock"）和不带（"Foo Common Stock (DE)"），
# 故分隔号可选、且先剥尾部注册地括号（"(DE)"/"(MD)"）。只裁已知描述性尾缀，不误伤公司名。
_US_STATE_PAREN = re.compile(r"\s*\((?:[A-Z]{2}|The)\)\s*$")
# 只裁「多词、明确」的股份类别短语（Common Stock / Ordinary Shares / ADR 等），可带或
# 不带分隔号。刻意不放裸单词分支（Unit/Right/Warrant…）——它们会误吃以此开头的公司名
# （曾把 "UnitedHealth…" 的 "Unit.*" 整条吞空）。这类证券在普通股池里也极少见。
_US_NAME_TAIL = re.compile(
    r"\s*(?:-\s*)?("
    r"(?:Class\s+[A-Z]\s+)?Common Stock"
    r"|(?:Class\s+[A-Z]\s+)?Ordinary Shares?"
    r"|(?:Class\s+[A-Z]\s+)?Common Shares?"
    r"|(?:American\s+)?Depositary Shar\w*(?:\s+\w+){0,3}"
    r"|(?:American\s+)?Depositary Receipts?"
    r")\s*$",
    re.IGNORECASE,
)
# 仅当有分隔号 " - " 时才裁的通用尾缀（避免误吃公司名）。
_US_DASH_TAIL = re.compile(
    r"\s+-\s+(Warrants?|Rights?|Units?|Preferred\b.*|Notes?\b.*|Depositary\b.*)\s*$",
    re.IGNORECASE,
)


def _clean_us_name(raw: str) -> str:
    """裁掉美股证券名里的股份类别尾缀 + 注册地括号，返回精简常用名。"""
    name = _US_STATE_PAREN.sub("", raw.strip())
    name = _US_DASH_TAIL.sub("", name).strip()
    name = _US_NAME_TAIL.sub("", name).strip()
    name = _US_STATE_PAREN.sub("", name).strip()  # 括号可能在类别词之前，再剥一次
    return name or raw.strip()


def _fetch_us_names() -> Dict[str, str]:
    """从 nasdaqtrader 官方目录拉全市场美股 Symbol→常用名。"""
    names: Dict[str, str] = {}
    for url in _NASDAQ_FILES:
        try:
            data = urllib.request.urlopen(url, timeout=30).read().decode("latin-1")
        except Exception as exc:  # noqa: BLE001 —— 构建脚本，单源失败降级即可
            logger.warning("[gen_names] 美股源下载失败 %s：%s", url, exc)
            continue
        lines = data.splitlines()
        if not lines:
            continue
        header = lines[0].split("|")
        try:
            sym_idx = header.index("Symbol") if "Symbol" in header else header.index("ACT Symbol")
            name_idx = header.index("Security Name")
            test_idx = header.index("Test Issue") if "Test Issue" in header else -1
        except ValueError:
            logger.warning("[gen_names] 美股源表头异常：%s", header)
            continue
        for line in lines[1:]:
            if line.startswith("File Creation Time"):
                continue
            cols = line.split("|")
            if len(cols) <= name_idx:
                continue
            if test_idx >= 0 and cols[test_idx].strip().upper() == "Y":
                continue
            symbol = cols[sym_idx].strip().upper()
            if not symbol or not symbol.isalnum():
                continue
            names[symbol] = _clean_us_name(cols[name_idx])
    logger.info("[gen_names] 美股 %d 只", len(names))
    return names


def _fetch_a_share_names(fmt) -> Dict[str, str]:
    """akshare 拉全 A 股 code→name，键格式化为 yfinance 后缀形式。"""
    import akshare as ak

    names: Dict[str, str] = {}
    try:
        df = ak.stock_info_a_code_name()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[gen_names] A 股源失败：%s", exc)
        return names
    for rec in df.to_dict("records"):
        code = str(rec.get("code", "")).strip()
        name = str(rec.get("name", "")).strip().replace(" ", "")
        if not code or not name:
            continue
        names[fmt(code)] = name
    logger.info("[gen_names] A 股 %d 只", len(names))
    return names


def _fetch_hk_names(fmt) -> Dict[str, str]:
    """港股 code→name：多个 akshare 端点带重试合并（eastmoney 港股接口时有瞬断）。"""
    import akshare as ak

    names: Dict[str, str] = {}
    sources = ["stock_hk_famous_spot_em", "stock_hk_ggt_components_em", "stock_hk_spot_em"]
    for fn in sources:
        for attempt in range(3):
            try:
                df = getattr(ak, fn)()
                for rec in df.to_dict("records"):
                    code = str(rec.get("代码", "")).strip()
                    name = str(rec.get("名称", "")).strip()
                    if not code or not name:
                        continue
                    names.setdefault(fmt(code), name)
                logger.info("[gen_names] 港股源 %s OK，累计 %d 只", fn, len(names))
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[gen_names] 港股源 %s 第 %d 次失败：%s", fn, attempt + 1, type(exc).__name__)
                time.sleep(2)
    return names


def _load_universe_tickers() -> List[str]:
    """读用户当前股票池的原始代码（用于兜底补名，保证用户实际标的都有名字）。"""
    if not _UNIVERSE_PATH.exists():
        return []
    try:
        data = json.loads(_UNIVERSE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [x["ticker"] for x in data if isinstance(x, dict) and x.get("ticker")]


def _yf_name(formatted: str) -> Optional[str]:
    """yfinance ``.info`` 取单只常用名（兜底用，构建期慢可接受）。"""
    import yfinance as yf

    try:
        info = yf.Ticker(formatted).info
        if not isinstance(info, dict):
            return None
        return info.get("shortName") or info.get("longName") or None
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from stock_bot.valuation_engine import format_universal_ticker, is_crypto_ticker

    def fmt(code: str) -> str:
        return format_universal_ticker(code)

    names: Dict[str, str] = {}
    names.update(_fetch_us_names())
    names.update(_fetch_a_share_names(fmt))
    names.update(_fetch_hk_names(fmt))

    # 兜底：用户股票池里任何仍缺名的（多为港股瞬断没拉到），yfinance 逐只补
    missing = []
    for t in _load_universe_tickers():
        if is_crypto_ticker(t):
            continue
        key = fmt(t)
        if key not in names:
            missing.append(key)
    if missing:
        logger.info("[gen_names] 用户池缺名 %d 只，yfinance 兜底：%s", len(missing), missing)
        for key in missing:
            nm = _yf_name(key)
            if nm:
                names[key] = nm
                logger.info("[gen_names]   %s → %s", key, nm)
            else:
                logger.warning("[gen_names]   %s 兜底失败，将显示代码", key)

    payload = {
        "source": "nasdaqtrader(US) + akshare stock_info_a_code_name(A) + akshare hk spot(HK) + yfinance fallback",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(names),
        "names": dict(sorted(names.items())),
    }
    tmp = _OUT_PATH.with_suffix(_OUT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_OUT_PATH)
    logger.info("[gen_names] 已写入 %s（%d 只）", _OUT_PATH, len(names))


if __name__ == "__main__":
    main()

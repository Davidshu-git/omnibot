"""
Query API + ingest triggers.

All responses are based on the unified schema; no source-specific fields leak here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select, distinct, String, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db, AsyncSessionLocal
from app.db.models import Agent, DataSource, Event, Project, Session
from app.config import settings

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)

BOT_CHAT_PROJECTS = {"stock-bot", "ehs-bot", "mhxy-bot"}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Pay-per-use model cost config (元 / 百万 tokens)
# ---------------------------------------------------------------------------

_COST_CONFIG: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {
        "input_per_m":     1.0,
        "cache_hit_per_m": 0.02,
        "output_per_m":    2.0,
    },
    "deepseek-v4-pro": {
        "input_per_m":     3.0,
        "cache_hit_per_m": 0.025,
        "output_per_m":    6.0,
    },
    "qwen3-vl-plus": {
        "input_per_m":     1.0,
        "cache_hit_per_m": 0.0,
        "output_per_m":    10.0,
    },
    "qwen3-vl-flash": {
        "input_per_m":     0.15,
        "cache_hit_per_m": 0.0,
        "output_per_m":    1.5,
    },
}


def _calc_cost(
    model: str,
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
) -> float | None:
    cfg = _COST_CONFIG.get(model)
    if cfg is None:
        return None
    non_cached = max(0, input_tokens - cache_read_tokens)
    return (
        non_cached          * cfg["input_per_m"]     / 1_000_000
        + cache_read_tokens * cfg["cache_hit_per_m"] / 1_000_000
        + output_tokens     * cfg["output_per_m"]    / 1_000_000
    )


# SSE 订阅者队列
_sse_subscribers: list[asyncio.Queue] = []


def _broadcast_event(event: str):
    for q in _sse_subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _broadcast_ingest():
    """ingest 完成后广播通知所有 SSE 订阅者。"""
    _broadcast_event("ingest")


async def notify_executor_status_changed() -> dict:
    """executor status file changed; notify SSE subscribers to refresh it."""
    _broadcast_event("executor_status")
    return {"event": "executor_status"}


@router.get("/stream")
async def sse_stream():
    """SSE 端点：日志有更新时推送 ingest 事件，前端订阅后自动刷新。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    _sse_subscribers.append(q)

    async def event_generator():
        try:
            yield "data: connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # 保活
        finally:
            try:
                _sse_subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@router.get("/projects")
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.is_active == True))
    projects = result.scalars().all()
    return [
        {
            "id": p.id,
            "display_name": p.display_name,
            "source_type": p.source_type,
            "created_at": p.created_at,
        }
        for p in projects
    ]


@router.get("/projects/{project_id}/agents")
async def list_agents(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Agent).where(Agent.project_id == project_id)
    )
    agents = result.scalars().all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "display_name": a.display_name,
            "kind": a.kind,
        }
        for a in agents
    ]


# ---------------------------------------------------------------------------
# Runtime models — 读取各 Bot 当前配置的文本模型 / 视觉模型
# ---------------------------------------------------------------------------

_RUNTIME_MODEL_FIELDS = ("model_key", "model", "display_name", "provider", "updated_at")

# 各 project 全量可用模型（与 core/model_registry.py 保持同步）
_AVAILABLE_TEXT_MODELS: dict[str, list[dict]] = {
    "mhxy": [
        {"key": "minimax",  "display_name": "MiniMax M2.7",      "provider": "minimax"},
        {"key": "qwen",     "display_name": "Qwen 3.5 Plus",     "provider": "dashscope"},
        {"key": "qwen36",   "display_name": "Qwen 3.6 Plus",     "provider": "dashscope"},
        {"key": "deepseek", "display_name": "DeepSeek V4 Flash", "provider": "deepseek"},
    ],
    "stock-bot": [
        {"key": "minimax",  "display_name": "MiniMax M2.7",      "provider": "minimax"},
        {"key": "qwen",     "display_name": "Qwen 3.5 Plus",     "provider": "dashscope"},
        {"key": "qwen36",   "display_name": "Qwen 3.6 Plus",     "provider": "dashscope"},
        {"key": "deepseek", "display_name": "DeepSeek V4 Flash", "provider": "deepseek"},
        {"key": "deepseek-pro", "display_name": "DeepSeek V4 Pro", "provider": "deepseek"},
    ],
    "ehs-bot": [
        {"key": "minimax",  "display_name": "MiniMax M2.7",      "provider": "minimax"},
        {"key": "qwen",     "display_name": "Qwen 3.5 Plus",     "provider": "dashscope"},
        {"key": "qwen36",   "display_name": "Qwen 3.6 Plus",     "provider": "dashscope"},
        {"key": "deepseek", "display_name": "DeepSeek V4 Flash", "provider": "deepseek"},
        {"key": "deepseek-pro", "display_name": "DeepSeek V4 Pro", "provider": "deepseek"},
    ],
}

_AVAILABLE_VL_MODELS: dict[str, list[dict]] = {
    "mhxy": [
        {"key": "plus",  "display_name": "Qwen3-VL Plus",  "provider": "dashscope"},
        {"key": "flash", "display_name": "Qwen3-VL Flash", "provider": "dashscope"},
    ],
}

# 盘后日报专属可切换模型（仅 stock-bot 有日报）。与文本模型同款列表。
_AVAILABLE_DAILY_MODELS: dict[str, list[dict]] = {
    "stock-bot": _AVAILABLE_TEXT_MODELS["stock-bot"],
}


def _read_settings_safe(path: Path) -> dict | None:
    """安全读取 settings JSON，文件不存在 / 损坏 / 字段缺失返回 None。"""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        values = {field: data.get(field) for field in _RUNTIME_MODEL_FIELDS}
        if not all(isinstance(value, str) and value for value in values.values()):
            return None
        return values
    except Exception:
        return None


@router.get("/projects/runtime-models")
async def projects_runtime_models():
    """读取每个 project 的 model_settings.json / vl_model_settings.json，
    返回当前配置的文本模型 / 视觉模型，以及各 project 的全量可用模型列表。"""
    runtime_dirs = {
        "mhxy": os.getenv("RUNTIME_DIR_MHXY") or "/runtime/mhxy",
        "stock-bot": os.getenv("RUNTIME_DIR_STOCK_BOT") or "/runtime/stock-bot",
        "ehs-bot": os.getenv("RUNTIME_DIR_EHS_BOT") or "/runtime/ehs-bot",
    }
    out = []
    for project_id, base in runtime_dirs.items():
        text = _read_settings_safe(Path(base) / "model_settings.json")
        vl = _read_settings_safe(Path(base) / "vl_model_settings.json")
        daily = _read_settings_safe(Path(base) / "daily_model_settings.json")
        out.append({
            "project_id": project_id,
            "text_model": text,
            "vl_model": vl,
            "daily_model": daily,
            "available_text_models": _AVAILABLE_TEXT_MODELS.get(project_id, []),
            "available_vl_models": _AVAILABLE_VL_MODELS.get(project_id, []),
            "available_daily_models": _AVAILABLE_DAILY_MODELS.get(project_id, []),
        })
    return out


# ---------------------------------------------------------------------------
# Agent-generated artifact files (read-only, served from runtime mounts)
# ---------------------------------------------------------------------------

@router.get("/files/stock/{filename}")
async def stock_chart_file(filename: str):
    """返回 stock bot 在 agent_workspace 下生成的图片（如 K 线走势图）。

    obs-api 以只读方式挂载了 stock bot 的 data 目录（/runtime/stock-bot），
    时间线 tool_result 的 meta.file_name 指向这里的文件，前端 <img> 按需拉取。

    Args:
        filename: 仅文件名（无路径分隔符），如 ``NVDA_30d_chart.png``。

    Raises:
        HTTPException: 路径越界（400）或文件不存在（404）。
    """
    base = (Path(os.getenv("RUNTIME_DIR_STOCK_BOT") or "/runtime/stock-bot") / "agent_workspace").resolve()
    target = (base / filename).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, media_type="image/png")


# ---------------------------------------------------------------------------
# 投资总控台（stock-bot 组合快照，只读自挂载的 snapshots/portfolio.jsonl）
# ---------------------------------------------------------------------------

def _portfolio_snapshot_path() -> Path:
    """组合快照 JSONL 在 obs 容器内的只读路径。"""
    base = Path(os.getenv("RUNTIME_DIR_STOCK_BOT") or "/runtime/stock-bot")
    return base / "snapshots" / "portfolio.jsonl"


def _read_portfolio_snapshots() -> list[dict]:
    """读取全部组合快照（按 date 升序）。

    文件由 stock_bot.snapshot 每日写入（每日一行）。obs 仅做只读消费，
    不参与任何财务计算。坏行跳过，文件缺失返回空列表。

    Returns:
        list[dict]: 快照列表，按 ``date`` 升序排列。
    """
    path = _portfolio_snapshot_path()
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("读取组合快照失败：%s", exc)
        return []
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def _portfolio_pricing_error_tickers(row: dict) -> list[str]:
    """提取单条快照中取价失败的持仓代码。"""
    tickers: list[str] = []
    for holding in row.get("holdings", []):
        if isinstance(holding, dict) and holding.get("error"):
            ticker = holding.get("ticker")
            tickers.append(str(ticker) if ticker else "UNKNOWN")
    return tickers


@router.get("/portfolio/latest")
async def portfolio_latest():
    """返回最新一条组合快照（投资总控台「当下快照」数据源）。

    Returns:
        dict: 最新快照；无任何快照时返回 ``{"available": False}``。
    """
    rows = _read_portfolio_snapshots()
    if not rows:
        return {"available": False}
    latest = rows[-1]
    errored_tickers = _portfolio_pricing_error_tickers(latest)
    return {
        "available": True,
        **latest,
        "has_pricing_error": bool(errored_tickers),
        "errored_tickers": errored_tickers,
    }


@router.get("/portfolio/history")
async def portfolio_history(days: int = Query(default=90, ge=1, le=730)):
    """返回最近 N 天的组合快照精简序列（为后续净值曲线预留）。

    Args:
        days: 回溯天数，默认 90，最大 730。

    Returns:
        dict: ``{"points": [...], "excluded_points": [...]}``。含持仓取价错误的
        快照会被剔除，避免 BTC 等单项取价失败把净值走势画成假跳水。
    """
    rows = _read_portfolio_snapshots()[-days:]
    clean_rows: list[dict] = []
    excluded_points: list[dict] = []
    for row in rows:
        errored_tickers = _portfolio_pricing_error_tickers(row)
        if errored_tickers:
            excluded_points.append({
                "date": row.get("date"),
                "errored_tickers": errored_tickers,
            })
            continue
        clean_rows.append(row)

    points = [
        {
            "date": r.get("date"),
            "total_market_value": r.get("total_market_value", 0.0),
            "total_profit_loss": r.get("total_profit_loss", 0.0),
            "profit_loss_percent": r.get("profit_loss_percent", 0.0),
            "securities_total_cny": r.get("securities_total_cny", 0.0),
            "cash_total_cny": r.get("cash_total_cny", 0.0),
        }
        for r in clean_rows
    ]
    return {
        "points": points,
        "excluded_points": excluded_points,
        "excluded_count": len(excluded_points),
    }


def _portfolio_returns_path() -> Path:
    """窗口 TWR（returns.json）在 obs 容器内的只读路径。"""
    base = Path(os.getenv("RUNTIME_DIR_STOCK_BOT") or "/runtime/stock-bot")
    return base / "snapshots" / "returns.json"


@router.get("/portfolio/returns")
async def portfolio_returns():
    """返回各时间窗收益率与可用性（投资总控台窗口点亮数据源）。

    两个互补口径：证券口径 **TWR**（twr_engine，时间加权）与账户口径 **MWR**
    （mwr_engine，资金加权 Modified Dietz + 年化 XIRR）。由 stock_bot.snapshot 每次落
    快照时算出并写 returns.json，obs 仅只读透传、不做任何财务计算。文件缺失（尚未生成）
    时优雅降级为「全窗不可用」，前端据此渲染灰态而非报错。

    Returns:
        dict: ``{"available": bool, ...returns字段}``。字段见 twr_engine.compute_windowed_returns
            与 mwr_engine.compute_account_mwr（后者提供每窗 mwr_* 字段）。
    """
    path = _portfolio_returns_path()
    if not path.is_file():
        return {"available": False}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取窗口 TWR 失败：%s", exc)
        return {"available": False}
    if not isinstance(data, dict):
        return {"available": False}
    return {"available": True, **data}


def _watchlist_path() -> Path:
    """自选观察清单 JSON 在 obs 容器内的只读路径（同 portfolio 快照，直读挂载文件）。"""
    base = Path(os.getenv("RUNTIME_DIR_STOCK_BOT") or "/runtime/stock-bot")
    return base / "memory" / "watchlist.json"


@router.get("/portfolio/watchlist")
async def portfolio_watchlist():
    """返回自选观察清单（投资总控台「👀 观察清单」数据源，直读挂载文件）。

    观察清单是 0 持仓的纯跟踪位，与估值 / 净值无关。**读**走 obs 直读文件（不依赖
    live bot），**增删**走 ``/external/{project}/watchlist-add|remove`` 代理 live bot。

    Returns:
        dict: ``{"items": [{"ticker", "note", "added_at"}, ...]}``；文件缺失 / 损坏返回空列表。
    """
    path = _watchlist_path()
    if not path.is_file():
        return {"items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取观察清单失败：%s", exc)
        return {"items": []}
    return {"items": data if isinstance(data, list) else []}


# ---------------------------------------------------------------------------
# External service status
# ---------------------------------------------------------------------------

@router.get("/external/mhxy-executor/instances")
async def mhxy_executor_instances():
    """Merge app_health from executor status with instances.json metadata (school, group role)."""
    status_path = Path(settings.mhxy_executor_status_file)
    app_health: list[dict] = []
    app_health_checked_at: str | None = None
    if status_path.exists():
        try:
            raw = json.loads(status_path.read_text(encoding="utf-8"))
            app_health = raw.get("app_health") or []
            app_health_checked_at = raw.get("app_health_checked_at")
        except Exception:
            pass

    instances_path = Path(settings.mhxy_instances_file)
    meta: dict[str, dict] = {}
    if instances_path.exists():
        try:
            inst_data = json.loads(instances_path.read_text(encoding="utf-8"))
            for inst in inst_data.get("instances", []):
                port = str(inst.get("port", ""))
                if port:
                    meta[port] = {"school": inst.get("school", ""), "role": "standalone", "group_id": None}
            for gid, group in enumerate(inst_data.get("groups", [])):
                leader = group.get("leader", {})
                lp = str(leader.get("port", ""))
                if lp in meta:
                    meta[lp]["role"] = "leader"
                    meta[lp]["group_id"] = gid
                for member in group.get("members", []):
                    mp = str(member.get("port", ""))
                    if mp in meta:
                        meta[mp]["role"] = "member"
                        meta[mp]["group_id"] = gid
        except Exception:
            pass

    result = []
    for item in app_health:
        port = str(item.get("port", ""))
        m = meta.get(port, {})
        result.append({
            "port": port,
            "school": m.get("school", ""),
            "role": m.get("role", ""),
            "group_id": m.get("group_id"),
            "healthy": item.get("healthy"),
            "adb": item.get("adb"),
            "screenshot": item.get("screenshot"),
            "ocr": item.get("ocr"),
            "latency_ms": item.get("latency_ms"),
            "error": item.get("error"),
        })

    return {"instances": result, "app_health_checked_at": app_health_checked_at}


@router.get("/external/mhxy-executor/screenshot")
async def mhxy_executor_screenshot(port: str = Query(..., description="Emulator port, e.g. 5555")):
    """Proxy /screenshot to the Windows executor and return base64 PNG."""
    import httpx as _httpx
    executor_url = settings.mhxy_executor_url.rstrip("/")
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{executor_url}/screenshot", json={"port": port})
            r.raise_for_status()
            data = r.json()
    except _httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Executor HTTP error: {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach executor: {exc}")
    image_b64 = data.get("image_b64")
    if not image_b64:
        raise HTTPException(status_code=502, detail="Executor returned no image")
    return {"port": port, "image_b64": image_b64}


@router.post("/external/mhxy-executor/batch-tap")
async def mhxy_executor_batch_tap(body: dict):
    """Proxy batch-tap to the Windows executor. body: {ports: [str], px: int, py: int}"""
    import httpx as _httpx
    ports = body.get("ports") or []
    px = body.get("px")
    py = body.get("py")
    if px is None or py is None:
        raise HTTPException(status_code=400, detail="px and py are required")
    if not ports:
        # Fall back to all known ports from instances
        status_path = Path(settings.mhxy_executor_status_file)
        if status_path.exists():
            try:
                raw = json.loads(status_path.read_text(encoding="utf-8"))
                ports = [str(item.get("port", "")) for item in (raw.get("app_health") or []) if item.get("port")]
            except Exception:
                pass
    if not ports:
        raise HTTPException(status_code=400, detail="No ports specified and none found in status file")
    executor_url = settings.mhxy_executor_url.rstrip("/")
    try:
        async with _httpx.AsyncClient(timeout=len(ports) * 2 + 10) as client:
            r = await client.post(f"{executor_url}/batch_tap", json={"ports": ports, "px": int(px), "py": int(py)})
            r.raise_for_status()
            return r.json()
    except _httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Executor HTTP error: {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach executor: {exc}")


@router.post("/external/mhxy-executor/batch-swipe")
async def mhxy_executor_batch_swipe(body: dict):
    """Proxy batch-swipe to the Windows executor.

    body: {ports: [str], x1: int, y1: int, x2: int, y2: int, duration_ms?: int}
    """
    import httpx as _httpx
    ports = body.get("ports") or []
    x1 = body.get("x1")
    y1 = body.get("y1")
    x2 = body.get("x2")
    y2 = body.get("y2")
    duration_ms = int(body.get("duration_ms") or 300)
    if x1 is None or y1 is None or x2 is None or y2 is None:
        raise HTTPException(status_code=400, detail="x1, y1, x2 and y2 are required")
    if duration_ms < 50 or duration_ms > 5000:
        raise HTTPException(status_code=400, detail="duration_ms must be between 50 and 5000")
    if not ports:
        status_path = Path(settings.mhxy_executor_status_file)
        if status_path.exists():
            try:
                raw = json.loads(status_path.read_text(encoding="utf-8"))
                ports = [str(item.get("port", "")) for item in (raw.get("app_health") or []) if item.get("port")]
            except Exception:
                pass
    if not ports:
        raise HTTPException(status_code=400, detail="No ports specified and none found in status file")
    executor_url = settings.mhxy_executor_url.rstrip("/")
    try:
        async with _httpx.AsyncClient(timeout=len(ports) * (duration_ms / 1000 + 2) + 10) as client:
            r = await client.post(
                f"{executor_url}/batch_swipe",
                json={
                    "ports": ports,
                    "x1": int(x1),
                    "y1": int(y1),
                    "x2": int(x2),
                    "y2": int(y2),
                    "duration_ms": duration_ms,
                },
            )
            r.raise_for_status()
            return r.json()
    except _httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Executor HTTP error: {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach executor: {exc}")


async def _post_chat_ingest(project: str, ingest_fn) -> None:
    """Best-effort ingest after an obs→bot chat turn, decoupled from the response.

    Args:
        project: Bot project key, used only for log context.
        ingest_fn: One of the run_*_ingest coroutines; broadcasts internally
            when it inserts events.

    Returns:
        None.
    """
    try:
        await ingest_fn(force=False)
    except Exception:
        logger.exception("[obs_chat] post-chat ingest failed for project=%s", project)


@router.post("/external/{project}/chat")
async def proxy_bot_chat(project: str, body: dict):
    """Proxy obs timeline text input to the live bot process."""
    import httpx as _httpx

    urls = {
        "stock-bot": settings.stock_bot_chat_url,
        "ehs-bot": settings.ehs_bot_chat_url,
        "mhxy-bot": settings.mhxy_bot_chat_url,
    }
    ingest_fns = {
        "stock-bot": run_stock_bot_ingest,
        "ehs-bot": run_ehs_bot_ingest,
        "mhxy-bot": run_mhxy_ingest,
    }

    if project not in BOT_CHAT_PROJECTS:
        raise HTTPException(status_code=404, detail="Unknown bot project")
    if not settings.obs_bot_chat_token:
        raise HTTPException(status_code=503, detail="OBS_BOT_CHAT_TOKEN is not configured")

    user_id = body.get("user_id")
    text = body.get("text")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise HTTPException(status_code=422, detail="user_id must be an integer")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="text must be a non-empty string")

    bot_url = urls[project].rstrip("/")
    try:
        async with _httpx.AsyncClient(timeout=110) as client:
            r = await client.post(
                f"{bot_url}/chat",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
                json={"user_id": user_id, "text": text.strip()},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    data = r.json()
    # Fire-and-forget: the bot already persisted this turn to its JSONL. Awaiting
    # the ingest here would hold the chat response behind the global _ingest_lock,
    # which the high-frequency mhxy-executor ingest can monopolize — that is what
    # makes the timeline chat hang past the client timeout. The watcher + this
    # background task pick the turn up and SSE-refresh the timeline anyway.
    asyncio.create_task(_post_chat_ingest(project, ingest_fns[project]))
    return data


@router.post("/external/{project}/switch-model")
async def proxy_switch_model(project: str, body: dict):
    """Proxy a model-switch request to the live bot process and SSE-refresh obs.

    The runtime dirs are mounted read-only into this container, and writing
    model_settings.json alone would not update the bot's in-memory registry
    anyway. So the switch is delegated to the live bot via its embedded HTTP
    server (same channel/token as the timeline chat proxy).
    """
    import httpx as _httpx

    urls = {
        "stock-bot": settings.stock_bot_chat_url,
        "ehs-bot": settings.ehs_bot_chat_url,
        "mhxy-bot": settings.mhxy_bot_chat_url,
    }

    if project not in BOT_CHAT_PROJECTS:
        raise HTTPException(status_code=404, detail="Unknown bot project")
    if not settings.obs_bot_chat_token:
        raise HTTPException(status_code=503, detail="OBS_BOT_CHAT_TOKEN is not configured")

    kind = body.get("kind", "text")
    model_key = body.get("model_key")
    if kind not in ("text", "vl", "daily"):
        raise HTTPException(status_code=422, detail="kind must be 'text', 'vl' or 'daily'")
    if not isinstance(model_key, str) or not model_key.strip():
        raise HTTPException(status_code=422, detail="model_key must be a non-empty string")

    bot_url = urls[project].rstrip("/")
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{bot_url}/switch-model",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
                json={"kind": kind, "model_key": model_key.strip()},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    # Tell every connected obs client to re-pull runtime-models. index.tsx
    # already listens for this event but nothing emitted it until now.
    _broadcast_event("model_switched")
    return r.json()


@router.post("/external/{project}/refresh-portfolio")
async def proxy_refresh_portfolio(project: str):
    """Trigger the live stock bot to re-fetch prices and rewrite its portfolio snapshot.

    The runtime snapshot file is mounted read-only into obs, and only stock_bot has
    the valuation engine + price deps. So the recompute is delegated to the live bot
    via its embedded HTTP server (same channel/token as chat / switch-model).
    """
    import httpx as _httpx

    urls = {
        "stock-bot": settings.stock_bot_chat_url,
        "ehs-bot": settings.ehs_bot_chat_url,
        "mhxy-bot": settings.mhxy_bot_chat_url,
    }

    if project not in BOT_CHAT_PROJECTS:
        raise HTTPException(status_code=404, detail="Unknown bot project")
    if not settings.obs_bot_chat_token:
        raise HTTPException(status_code=503, detail="OBS_BOT_CHAT_TOKEN is not configured")

    bot_url = urls[project].rstrip("/")
    try:
        # 取价 + 落盘可能耗时十几秒（yfinance/akshare 重试），超时给足 45s。
        async with _httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                f"{bot_url}/refresh-portfolio",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.json() or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    # 广播让所有连接的 obs 客户端刷新总控台快照（非触发方也能看到最新值）。
    _broadcast_event("portfolio_refreshed")
    return r.json()


@router.post("/external/{project}/stock-trend")
async def proxy_stock_trend(project: str, body: dict):
    """Proxy a per-ticker trend query (price + MA20/60/250) to the live stock bot.

    Read-only lookup — the runtime price data lives only in the stock_bot container
    (yfinance/akshare deps), so this is delegated via the same embedded HTTP channel
    as chat / switch-model / refresh-portfolio. No SSE broadcast: this only matters
    to the requesting client, not the whole obs session.
    """
    import httpx as _httpx

    urls = {
        "stock-bot": settings.stock_bot_chat_url,
        "ehs-bot": settings.ehs_bot_chat_url,
        "mhxy-bot": settings.mhxy_bot_chat_url,
    }

    if project not in BOT_CHAT_PROJECTS:
        raise HTTPException(status_code=404, detail="Unknown bot project")
    if not settings.obs_bot_chat_token:
        raise HTTPException(status_code=503, detail="OBS_BOT_CHAT_TOKEN is not configured")

    ticker = body.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise HTTPException(status_code=422, detail="ticker must be a non-empty string")
    payload = {"ticker": ticker.strip()}
    # 显示窗口（可选）：透传给 bot，白名单由估值引擎 TREND_WINDOWS 校验。
    period = body.get("period")
    if period is not None:
        if not isinstance(period, str) or not period.strip():
            raise HTTPException(status_code=422, detail="period must be a non-empty string")
        payload["period"] = period.strip()

    bot_url = urls[project].rstrip("/")
    try:
        # 单次 yfinance 拉取 + 均线计算，比重估值轻，20s 足够。
        async with _httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{bot_url}/stock-trend",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
                json=payload,
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.json() or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    return r.json()


def _screener_bot_url(project: str) -> str:
    """Resolve and validate the bot chat URL for a screener proxy call; raises HTTPException on failure."""
    urls = {
        "stock-bot": settings.stock_bot_chat_url,
        "ehs-bot": settings.ehs_bot_chat_url,
        "mhxy-bot": settings.mhxy_bot_chat_url,
    }
    if project not in BOT_CHAT_PROJECTS:
        raise HTTPException(status_code=404, detail="Unknown bot project")
    if not settings.obs_bot_chat_token:
        raise HTTPException(status_code=503, detail="OBS_BOT_CHAT_TOKEN is not configured")
    return urls[project].rstrip("/")


@router.post("/external/{project}/screener-start")
async def proxy_screener_start(project: str):
    """Trigger the live stock bot to start a background screener scan (fire-and-forget)."""
    import httpx as _httpx

    bot_url = _screener_bot_url(project)
    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{bot_url}/screener-start",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.json() or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    return r.json()


@router.post("/external/{project}/screener-status")
async def proxy_screener_status(project: str):
    """Poll the live stock bot for current screener scan progress/results.

    High-frequency polling target (obs front-end hits this every few seconds while a
    scan is running), so timeout is short — this only reads a status file, no yfinance
    calls happen on this path.
    """
    import httpx as _httpx

    bot_url = _screener_bot_url(project)
    try:
        async with _httpx.AsyncClient(timeout=5) as client:
            r = await client.post(
                f"{bot_url}/screener-status",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.json() or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    return r.json()


@router.post("/external/{project}/screener-universe")
async def proxy_screener_universe(project: str):
    """Read the current screener universe (ticker pool) from the live stock bot."""
    import httpx as _httpx

    bot_url = _screener_bot_url(project)
    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{bot_url}/screener-universe",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.json() or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    return r.json()


@router.post("/external/{project}/screener-universe-save")
async def proxy_screener_universe_save(project: str, body: dict):
    """Overwrite the screener universe (ticker pool) via the live stock bot."""
    import httpx as _httpx

    bot_url = _screener_bot_url(project)
    tickers = body.get("tickers")
    if not isinstance(tickers, list) or not all(isinstance(t, str) for t in tickers):
        raise HTTPException(status_code=422, detail="tickers must be a list of strings")

    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{bot_url}/screener-universe-save",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
                json={"tickers": tickers},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.json() or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    return r.json()


@router.post("/external/{project}/screener-preset")
async def proxy_screener_preset(project: str):
    """Read the bundled preset ticker pool (static resource shipped with the bot, not user data)."""
    import httpx as _httpx

    bot_url = _screener_bot_url(project)
    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{bot_url}/screener-preset",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.json() or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    return r.json()


@router.post("/external/{project}/watchlist-add")
async def proxy_watchlist_add(project: str, body: dict):
    """Add a ticker to the stock bot's watchlist via the live bot (obs is read-only)."""
    import httpx as _httpx

    bot_url = _screener_bot_url(project)
    ticker = body.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise HTTPException(status_code=422, detail="ticker must be a non-empty string")
    note = body.get("note", "")
    if not isinstance(note, str):
        raise HTTPException(status_code=422, detail="note must be a string")

    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{bot_url}/watchlist-add",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
                json={"ticker": ticker.strip(), "note": note},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.json() or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    return r.json()


@router.post("/external/{project}/watchlist-remove")
async def proxy_watchlist_remove(project: str, body: dict):
    """Remove a ticker from the stock bot's watchlist via the live bot (obs is read-only)."""
    import httpx as _httpx

    bot_url = _screener_bot_url(project)
    ticker = body.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise HTTPException(status_code=422, detail="ticker must be a non-empty string")

    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{bot_url}/watchlist-remove",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
                json={"ticker": ticker.strip()},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach bot chat service: {exc}")

    if r.status_code >= 400:
        detail: object
        try:
            detail = r.json().get("detail") or r.json() or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    return r.json()


@router.get("/external/mhxy-executor/status")
async def mhxy_executor_status():
    path = Path(settings.mhxy_executor_status_file)
    if not path.exists():
        return {
            "service": "mhxy_windows_executor",
            "status": "unknown",
            "stale": True,
            "error": f"status file not found: {path}",
        }

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "service": "mhxy_windows_executor",
            "status": "unknown",
            "stale": True,
            "error": f"status file unreadable: {exc}",
        }

    checked_at = _parse_dt(data.get("checked_at"))
    interval = int(data.get("interval_sec") or 60)
    stale_after = max(interval * 3, 180)
    age_sec = None
    stale = True
    if checked_at:
        age_sec = int((datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc)).total_seconds())
        stale = age_sec > stale_after

    if stale and data.get("status") == "healthy":
        data = {**data, "status": "stale"}
    data["stale"] = stale
    data["age_sec"] = age_sec
    data["status_file"] = str(path)
    return data


@router.post("/external/mhxy-executor/power")
async def mhxy_executor_power(body: dict):
    """Toggle the Windows executor on/off via the mhxy bot's embedded HTTP server.

    obs-api mounts the mhxy data dir read-only, so it cannot write the power
    flag itself. The mhxy bot container (which mounts the config dir RW) owns
    the file; the watchdog reads it each cycle and stops / lets-restart the
    executor accordingly. Same channel/token as /switch-model.
    """
    import httpx as _httpx

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="enabled must be a boolean")
    if not settings.obs_bot_chat_token:
        raise HTTPException(status_code=503, detail="OBS_BOT_CHAT_TOKEN is not configured")

    bot_url = settings.mhxy_bot_chat_url.rstrip("/")
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{bot_url}/executor-power",
                headers={"X-OBS-Token": settings.obs_bot_chat_token},
                json={"enabled": enabled},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach mhxy bot service: {exc}")

    if r.status_code >= 400:
        try:
            detail = r.json().get("detail") or r.text
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)

    _broadcast_event("executor_status")
    return r.json()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def list_sessions(
    project_id: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    # Sort by most recent event timestamp (last-active semantics).
    # Uses a correlated subquery on events(session_id, timestamp) index — no migration needed.
    # If sessions table grows large (>10k), consider adding a last_event_at column instead.
    from sqlalchemy import func, select as sa_select, nulls_last
    last_event_subq = (
        sa_select(func.max(Event.timestamp))
        .where(Event.session_id == Session.id)
        .correlate(Session)
        .scalar_subquery()
    )
    q = select(Session).order_by(nulls_last(last_event_subq.desc()))
    if project_id:
        q = q.where(Session.project_id == project_id)
    if agent_id:
        q = q.where(Session.agent_id == agent_id)
    if since:
        q = q.where(Session.started_at >= since)
    if until:
        q = q.where(Session.started_at <= until)
    q = q.limit(limit).offset(offset)

    result = await db.execute(q)
    sessions = result.scalars().all()
    return [
        {
            "id": s.id,
            "project_id": s.project_id,
            "agent_id": s.agent_id,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "status": s.status,
        }
        for s in sessions
    ]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "id": session.id,
        "project_id": session.project_id,
        "agent_id": session.agent_id,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "status": session.status,
        "metadata": session.metadata_,
    }


@router.get("/sessions/{session_id}/timeline")
async def session_timeline(
    session_id: str,
    limit: int = Query(200, le=500),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    events_result = await db.execute(
        select(Event)
        .where(Event.session_id == session_id)
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rounds_result = await db.execute(
        select(Event.trace_id, func.count().label("rounds"))
        .where(Event.session_id == session_id, Event.event_type == "model_call", Event.trace_id.isnot(None))
        .group_by(Event.trace_id)
    )
    events = events_result.scalars().all()
    rounds_by_trace = {row.trace_id: row.rounds for row in rounds_result}
    return {
        "events": [
            {
                "event_id": e.event_id,
                "project_id": e.project_id,
                "event_type": e.event_type,
                "timestamp": e.timestamp,
                "trace_id": e.trace_id,
                "run_id": e.run_id,
                "payload": e.payload_json,
                "extra": e.extra,
            }
            for e in events
        ],
        "rounds_by_trace": rounds_by_trace,
    }


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------

@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Event)
        .where(Event.trace_id == trace_id)
        .order_by(Event.timestamp, Event.id)
    )
    events = result.scalars().all()
    if not events:
        raise HTTPException(status_code=404, detail="Trace not found")
    total_cost: float | None = None
    for e in events:
        if e.event_type == "model_call" and e.payload_json:
            c = _calc_cost(
                e.payload_json.get("model") or "",
                e.payload_json.get("input_tokens") or 0,
                e.payload_json.get("cache_read_tokens") or 0,
                e.payload_json.get("output_tokens") or 0,
            )
            if c is not None:
                total_cost = (total_cost or 0.0) + c
    return {
        "trace_id": trace_id,
        "total_cost": total_cost,
        "events": [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "timestamp": e.timestamp,
                "session_id": e.session_id,
                "project_id": e.project_id,
                "trace_id": e.trace_id,
                "run_id": e.run_id,
                "payload": e.payload_json,
            }
            for e in events
        ],
    }


# ---------------------------------------------------------------------------
# Stats — tokens
# ---------------------------------------------------------------------------

@router.get("/stats/tokens/overview")
async def tokens_overview(
    project_id: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    q = select(
        func.sum(Event.payload_json["input_tokens"].as_integer()).label("input_tokens"),
        func.sum(Event.payload_json["output_tokens"].as_integer()).label("output_tokens"),
        func.sum(Event.payload_json["cache_read_tokens"].as_integer()).label("cache_read_tokens"),
        func.count().label("calls"),
    ).where(Event.event_type == "model_call")
    if project_id:
        q = q.where(Event.project_id == project_id)
    if since:
        q = q.where(Event.timestamp >= since)
    if until:
        q = q.where(Event.timestamp <= until)
    result = await db.execute(q)
    row = result.one()
    return {
        "input_tokens": row.input_tokens or 0,
        "output_tokens": row.output_tokens or 0,
        "cache_read_tokens": row.cache_read_tokens or 0,
        "calls": row.calls,
    }


@router.get("/stats/tokens/daily")
async def tokens_daily(
    project_id: Optional[str] = Query(None),
    days: int = Query(14),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day_col = func.date(Event.timestamp).label("date")
    base_filter = [Event.event_type == "model_call", Event.timestamp >= since]
    if project_id:
        base_filter.append(Event.project_id == project_id)

    # Token totals per day
    q = (
        select(
            day_col,
            func.sum(Event.payload_json["input_tokens"].as_integer()).label("input_tokens"),
            func.sum(Event.payload_json["output_tokens"].as_integer()).label("output_tokens"),
            func.count().label("calls"),
        )
        .where(*base_filter)
        .group_by(func.date(Event.timestamp))
        .order_by(func.date(Event.timestamp).desc())
    )
    result = await db.execute(q)
    rows = result.all()

    # Cost per day via (date, model) breakdown
    model_col = Event.payload_json["model"].as_string().label("model")
    cost_q = (
        select(
            func.date(Event.timestamp).label("date"),
            model_col,
            func.sum(Event.payload_json["input_tokens"].as_integer()).label("inp"),
            func.sum(Event.payload_json["cache_read_tokens"].as_integer()).label("cache"),
            func.sum(Event.payload_json["output_tokens"].as_integer()).label("out"),
        )
        .where(*base_filter)
        .group_by(func.date(Event.timestamp), model_col)
    )
    cost_result = await db.execute(cost_q)
    cost_by_date: dict[str, float] = {}
    model_costs_by_date: dict[str, dict[str, float]] = {}
    model_tokens_by_date: dict[str, dict[str, int]] = {}
    for cr in cost_result.all():
        date_str = str(cr.date)
        model = cr.model or "unknown"
        total_tok = (cr.inp or 0) + (cr.out or 0)
        model_tokens_by_date.setdefault(date_str, {})[model] = total_tok
        c = _calc_cost(model, cr.inp or 0, cr.cache or 0, cr.out or 0)
        if c is not None:
            cost_by_date[date_str] = (cost_by_date.get(date_str) or 0.0) + c
            model_costs_by_date.setdefault(date_str, {})[model] = c

    return [
        {
            "date": str(r.date),
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "calls": r.calls,
            "cost": cost_by_date.get(str(r.date)),
            "model_costs": [
                {"model": m, "cost": c}
                for m, c in (model_costs_by_date.get(str(r.date)) or {}).items()
            ],
            "model_tokens": [
                {"model": m, "total_tokens": t}
                for m, t in (model_tokens_by_date.get(str(r.date)) or {}).items()
            ],
        }
        for r in rows
    ]


@router.get("/stats/tokens/by-model")
async def tokens_by_model(
    project_id: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    model_col = Event.payload_json["model"].as_string().label("model")
    q = (
        select(
            model_col,
            func.sum(Event.payload_json["input_tokens"].as_integer()).label("input_tokens"),
            func.sum(Event.payload_json["output_tokens"].as_integer()).label("output_tokens"),
            func.sum(Event.payload_json["cache_read_tokens"].as_integer()).label("cache_read_tokens"),
            func.count().label("calls"),
        )
        .where(Event.event_type == "model_call")
        .group_by(model_col)
        .order_by(func.sum(Event.payload_json["input_tokens"].as_integer() + Event.payload_json["output_tokens"].as_integer()).desc())
    )
    if project_id:
        q = q.where(Event.project_id == project_id)
    if since:
        q = q.where(Event.timestamp >= since)
    if until:
        q = q.where(Event.timestamp <= until)
    result = await db.execute(q)
    rows = []
    for r in result.all():
        model = r.model or "unknown"
        inp = r.input_tokens or 0
        out = r.output_tokens or 0
        cache = r.cache_read_tokens or 0
        rows.append({
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache,
            "calls": r.calls,
            "cost": _calc_cost(model, inp, cache, out),
        })
    return rows


# ---------------------------------------------------------------------------
# Stats — tools
# ---------------------------------------------------------------------------

@router.get("/stats/tools")
async def tools_stats(
    project_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    tool_col = Event.payload_json["tool_name"].as_string().label("tool_name")
    q = (
        select(tool_col, func.count().label("calls"))
        .where(Event.event_type == "tool_call")
        .group_by(tool_col)
        .order_by(func.count().desc())
    )
    if project_id:
        q = q.where(Event.project_id == project_id)
    result = await db.execute(q)
    return [{"tool_name": r.tool_name, "calls": r.calls} for r in result.all()]


# ---------------------------------------------------------------------------
# Stats — instance diagnoses
# ---------------------------------------------------------------------------

@router.get("/stats/diagnoses")
async def diagnoses_stats(
    project_id: Optional[str] = Query("mhxy"),
    since: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate structured instance diagnosis metadata by code and state."""
    meta_kind = Event.payload_json["meta"]["kind"].as_string()
    code_col = Event.payload_json["meta"]["code"].as_string().label("code")
    state_col = Event.payload_json["meta"]["state"].as_string().label("state")
    needs_human = Event.payload_json["meta"]["needs_human"].as_boolean()

    base_filter = [
        Event.event_type == "tool_result",
        meta_kind == "instance_diagnosis",
    ]
    if project_id:
        base_filter.append(Event.project_id == project_id)
    if since:
        base_filter.append(Event.timestamp >= since)

    q = (
        select(
            code_col,
            state_col,
            func.count().label("n"),
            func.sum(case((needs_human, 1), else_=0)).label("needs_human"),
        )
        .where(*base_filter)
        .group_by(code_col, state_col)
        .order_by(func.count().desc())
    )
    result = await db.execute(q)
    return [
        {
            "code": r.code,
            "state": r.state,
            "count": r.n,
            "needs_human": int(r.needs_human or 0),
        }
        for r in result.all()
    ]


@router.get("/stats/diagnoses/recent")
async def diagnoses_recent(
    project_id: str = Query("mhxy"),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Return recent structured diagnosis events, including full step lists."""
    meta_kind = Event.payload_json["meta"]["kind"].as_string()
    q = (
        select(Event)
        .where(
            Event.event_type == "tool_result",
            meta_kind == "instance_diagnosis",
            Event.project_id == project_id,
        )
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(limit)
    )
    result = await db.execute(q)
    events = result.scalars().all()
    return [
        {
            "event_id": e.event_id,
            "session_id": e.session_id,
            "timestamp": e.timestamp,
            "trace_id": e.trace_id,
            "meta": (e.payload_json or {}).get("meta") or {},
        }
        for e in events
    ]


# ---------------------------------------------------------------------------
# Think
# ---------------------------------------------------------------------------

@router.get("/think")
async def list_think(
    project_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Event)
        .where(Event.event_type == "thought")
        .order_by(Event.timestamp.desc())
        .limit(limit)
    )
    if project_id:
        q = q.where(Event.project_id == project_id)
    if session_id:
        q = q.where(Event.session_id == session_id)
    result = await db.execute(q)
    events = result.scalars().all()
    return [
        {
            "event_id": e.event_id,
            "session_id": e.session_id,
            "timestamp": e.timestamp,
            "payload": e.payload_json,
        }
        for e in events
    ]


# ---------------------------------------------------------------------------
# Generic JSONL ingest — shared by all JSONL-based data sources
# ---------------------------------------------------------------------------

_ingest_lock = asyncio.Lock()


async def run_jsonl_ingest(
    *,
    project_id: str,
    source_type: str,
    display_name: str,
    log_dir: str,
    adapter,
    agents: list[dict],  # list of {id, name, display_name, kind}
    force: bool = False,
) -> dict:
    """
    Incremental JSONL ingest for any adapter that implements the discover/scan/load interface.
    Uses data_sources.last_sync_cursor (file mtime map) to skip unchanged files.
    Idempotent — event-level deduplication via ON CONFLICT DO NOTHING.
    """
    import json as _json
    from datetime import timezone
    from sqlalchemy import update as sa_update
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.ingestion.service import ingest_batch, upsert_session
    from app.db.models import DataSource

    async with _ingest_lock:
      async with AsyncSessionLocal() as db:
        await _ensure_project(db, project_id, display_name, source_type)
        for ag in agents:
            await _ensure_agent(db, ag["id"], project_id, ag["name"], ag["display_name"], ag["kind"])

        await db.execute(
            pg_insert(DataSource).values(
                project_id=project_id,
                source_type=source_type,
                display_name=display_name,
                enabled=True,
                config_json={"log_dir": log_dir},
            ).on_conflict_do_nothing()
        )
        await db.commit()

        ds_result = await db.execute(
            select(DataSource)
            .where(DataSource.project_id == project_id, DataSource.source_type == source_type)
            .order_by(DataSource.id.desc())
            .limit(1)
        )
        ds = ds_result.scalars().first()

        cursor: dict[str, Any] = {}
        if ds and ds.last_sync_cursor and not force:
            try:
                cursor = _json.loads(ds.last_sync_cursor)
            except Exception:
                cursor = {}

        total = {"raw_inserted": 0, "raw_updated": 0, "events_inserted": 0, "events_skipped": 0}
        sources_scanned = sources_skipped = 0
        new_cursor: dict[str, dict[str, Any]] = {}

        sources = adapter.discover_sources()
        for source in sources:
            st = os.stat(source.path)
            mtime = str(st.st_mtime)
            size = st.st_size
            new_cursor[source.path] = {"mtime": mtime, "size": size}

            # Backward-compatible: pre-existing cursors stored a bare mtime
            # string (no size). Treating "size unknown" as "changed" would force
            # a full re-ingest of every historical file on every run until a
            # size is recorded — and the run never completes to record one.
            # So a legacy cursor with an unchanged mtime still skips, exactly
            # like the old behavior; size only drives the tail-only resume.
            prev = cursor.get(source.path)
            if isinstance(prev, dict):
                prev_mtime = prev.get("mtime")
                prev_size = prev.get("size")
            else:
                prev_mtime, prev_size = prev, None

            if not force and prev_mtime == mtime and prev_size in (None, size):
                sources_skipped += 1
                continue

            # Append-only sources (e.g. the continuously-growing mhxy executor
            # file) resume from the previous end-of-file so each ingest parses
            # only the new tail instead of re-reading the whole file. Adapters
            # that ignore start_offset just re-read from 0; content-addressed
            # external_key + ON CONFLICT keeps both paths idempotent.
            if not force and isinstance(prev_size, int) and size >= prev_size:
                source.start_offset = prev_size

            sources_scanned += 1
            for session_ref in adapter.scan_sessions(source):
                # Determine agent_id: use session metadata if available, else first agent
                session_agent_id = (
                    session_ref.metadata.get("agent_id")
                    or (agents[0]["id"] if agents else None)
                )
                await upsert_session(
                    db,
                    session_id=session_ref.session_id,
                    project_id=project_id,
                    agent_id=session_agent_id,
                    external_session_id=session_ref.session_id,
                )

                raw_blobs, events = adapter.load_events(session_ref)

                for ev in events:
                    if ev.event_type.value == "session_started":
                        await upsert_session(
                            db,
                            session_id=session_ref.session_id,
                            project_id=project_id,
                            agent_id=session_agent_id,
                            external_session_id=session_ref.session_id,
                            started_at=ev.timestamp,
                        )
                        break

                counts = await ingest_batch(db, raw_blobs=raw_blobs, events=events)
                for k in total:
                    total[k] += counts[k]

        if ds is not None:
            await db.execute(
                sa_update(DataSource).where(DataSource.id == ds.id).values(
                    last_sync_cursor=_json.dumps(new_cursor),
                    last_sync_at=datetime.now(timezone.utc),
                    last_error=None,
                )
            )
            await db.commit()

        result = {
            "sources_total": len(sources),
            "sources_scanned": sources_scanned,
            "sources_skipped": sources_skipped,
            **total,
        }
        if total.get("events_inserted", 0) > 0:
            _broadcast_ingest()
        return result


# ---------------------------------------------------------------------------
# Per-source ingest wrappers — used by HTTP endpoints and startup/watcher
# ---------------------------------------------------------------------------

async def run_mhxy_ingest(force: bool = False) -> dict:
    from app.adapters.mhxy_jsonl import MhxyJsonlAdapter
    log_dir = os.getenv("MHXY_LOG_DIR", "/logs/mhxy/sessions")
    return await run_jsonl_ingest(
        project_id="mhxy",
        source_type="mhxy_jsonl",
        display_name="梦幻西游 Bot JSONL logs",
        log_dir=log_dir,
        adapter=MhxyJsonlAdapter(log_dir=log_dir),
        agents=[{"id": "mhxy-bot", "name": "mhxy-bot", "display_name": "梦幻西游 Bot", "kind": "bot"}],
        force=force,
    )


async def run_mhxy_executor_ingest(force: bool = False) -> dict:
    from app.adapters.mhxy_executor_jsonl import MhxyExecutorJsonlAdapter
    log_dir = os.getenv("MHXY_EXECUTOR_LOG_DIR", "/logs/omnibot/mhxy/executor")
    return await run_jsonl_ingest(
        project_id="mhxy",
        source_type="mhxy_executor_jsonl",
        display_name="MHXY Windows executor JSONL logs",
        log_dir=log_dir,
        adapter=MhxyExecutorJsonlAdapter(log_dir=log_dir),
        agents=[{"id": "windows-executor", "name": "windows-executor", "display_name": "Windows Executor", "kind": "executor"}],
        force=force,
    )


async def run_stock_bot_ingest(force: bool = False) -> dict:
    from app.adapters.omnibot_jsonl import OmnibotJsonlAdapter
    stock_dir = os.getenv("OMNIBOT_STOCK_LOG_DIR", "/logs/omnibot/stock/sessions")
    return await run_jsonl_ingest(
        project_id="stock-bot",
        source_type="omnibot_jsonl",
        display_name="OmniStock 量化助理",
        log_dir=stock_dir,
        adapter=OmnibotJsonlAdapter(log_dir=stock_dir, project_id="stock-bot"),
        agents=[{"id": "stock-bot", "name": "stock-bot", "display_name": "OmniStock 量化助理", "kind": "assistant"}],
        force=force,
    )


async def run_ehs_bot_ingest(force: bool = False) -> dict:
    from app.adapters.omnibot_jsonl import OmnibotJsonlAdapter
    ehs_dir = os.getenv("OMNIBOT_EHS_LOG_DIR", "/logs/omnibot/ehs/sessions")
    return await run_jsonl_ingest(
        project_id="ehs-bot",
        source_type="omnibot_jsonl",
        display_name="OmniEHS 安全合规助理",
        log_dir=ehs_dir,
        adapter=OmnibotJsonlAdapter(log_dir=ehs_dir, project_id="ehs-bot"),
        agents=[{"id": "ehs-bot", "name": "ehs-bot", "display_name": "OmniEHS 安全合规助理", "kind": "assistant"}],
        force=force,
    )


async def run_omnibot_ingest(force: bool = False) -> dict:
    r1 = await run_stock_bot_ingest(force=force)
    r2 = await run_ehs_bot_ingest(force=force)
    merged = {}
    for key in ("sources_total", "sources_scanned", "sources_skipped",
                "raw_inserted", "raw_updated", "events_inserted", "events_skipped"):
        merged[key] = r1.get(key, 0) + r2.get(key, 0)
    return merged


# ---------------------------------------------------------------------------
# HTTP ingest endpoints
# ---------------------------------------------------------------------------

@router.post("/ingest/mhxy")
async def ingest_mhxy(
    force: bool = Query(False, description="Force full re-scan even if file unchanged"),
):
    result = await run_mhxy_ingest(force=force)
    return {"status": "ok", **result}


@router.post("/ingest/mhxy-executor")
async def ingest_mhxy_executor(force: bool = Query(False)):
    result = await run_mhxy_executor_ingest(force=force)
    return {"status": "ok", **result}


@router.post("/ingest/stock-bot")
async def ingest_stock_bot(force: bool = Query(False)):
    result = await run_stock_bot_ingest(force=force)
    return {"status": "ok", **result}


@router.post("/ingest/ehs-bot")
async def ingest_ehs_bot(force: bool = Query(False)):
    result = await run_ehs_bot_ingest(force=force)
    return {"status": "ok", **result}


@router.post("/ingest/omnibot")
async def ingest_omnibot(force: bool = Query(False)):
    result = await run_omnibot_ingest(force=force)
    return {"status": "ok", **result}


# ---------------------------------------------------------------------------
# Stats — overview (per-project summary for the dashboard)
# ---------------------------------------------------------------------------

@router.get("/stats/overview")
async def stats_overview(db: AsyncSession = Depends(get_db)):
    """
    Returns a per-project summary: session count, today's sessions,
    token totals, last session time.
    """
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    today = datetime.now(CST).date()
    today_start_utc = datetime.combine(today, datetime.min.time()).replace(tzinfo=CST).astimezone(timezone.utc)
    today_end_utc = today_start_utc + timedelta(days=1)

    projects_result = await db.execute(select(Project).where(Project.is_active == True))
    projects = projects_result.scalars().all()
    if not projects:
        return []

    project_ids = [p.id for p in projects]

    # Batch 1: session counts + last_session per project
    session_agg_r = await db.execute(
        select(
            Session.project_id,
            func.count().label("total_sessions"),
            func.count().filter(
                Session.started_at >= today_start_utc,
                Session.started_at < today_end_utc,
            ).label("today_sessions"),
            func.max(Session.started_at).label("last_session_at"),
        )
        .where(Session.project_id.in_(project_ids))
        .group_by(Session.project_id)
    )
    session_stats = {r.project_id: r for r in session_agg_r.all()}

    # Batch 2: token totals + today_calls per project
    event_agg_r = await db.execute(
        select(
            Event.project_id,
            func.sum(Event.payload_json["input_tokens"].as_integer()).label("input_tokens"),
            func.sum(Event.payload_json["output_tokens"].as_integer()).label("output_tokens"),
            func.count().filter(
                Event.timestamp >= today_start_utc,
                Event.timestamp < today_end_utc,
            ).label("today_calls"),
        )
        .where(
            Event.event_type == "model_call",
            Event.project_id.in_(project_ids),
        )
        .group_by(Event.project_id)
    )
    event_stats = {r.project_id: r for r in event_agg_r.all()}

    # Batch 3: per-model cost breakdown per project
    model_col = Event.payload_json["model"].as_string().label("model")
    cost_agg_r = await db.execute(
        select(
            Event.project_id,
            model_col,
            func.sum(Event.payload_json["input_tokens"].as_integer()).label("inp"),
            func.sum(Event.payload_json["output_tokens"].as_integer()).label("out"),
            func.sum(Event.payload_json["cache_read_tokens"].as_integer()).label("cache"),
        )
        .where(
            Event.event_type == "model_call",
            Event.project_id.in_(project_ids),
        )
        .group_by(Event.project_id, model_col)
    )
    cost_by_project: dict[str, float] = {}
    for r in cost_agg_r.all():
        c = _calc_cost(r.model or "", r.inp or 0, r.cache or 0, r.out or 0)
        if c is not None:
            cost_by_project[r.project_id] = (cost_by_project.get(r.project_id) or 0.0) + c

    # Batch 4: latest session_id per project (excluding windows-executor)
    latest_session_r = await db.execute(
        select(Session.project_id, Session.id)
        .distinct(Session.project_id)
        .where(
            Session.project_id.in_(project_ids),
            Session.agent_id != "windows-executor",
        )
        .order_by(Session.project_id, Session.started_at.desc())
    )
    latest_session = {r.project_id: r.id for r in latest_session_r.all()}

    output = []
    for p in projects:
        ss = session_stats.get(p.id)
        es = event_stats.get(p.id)
        output.append({
            "project_id": p.id,
            "display_name": p.display_name,
            "total_sessions": ss.total_sessions if ss else 0,
            "today_sessions": ss.today_sessions if ss else 0,
            "today_calls": es.today_calls if es else 0,
            "last_session_at": ss.last_session_at if ss else None,
            "last_session_id": latest_session.get(p.id),
            "total_input_tokens": es.input_tokens or 0 if es else 0,
            "total_output_tokens": es.output_tokens or 0 if es else 0,
            "total_cost": cost_by_project.get(p.id),
        })

    return output


async def _ensure_project(db: AsyncSession, pid: str, display_name: str, source_type: str):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(Project).values(
        id=pid, display_name=display_name, source_type=source_type
    ).on_conflict_do_nothing(index_elements=["id"])
    await db.execute(stmt)
    await db.commit()


async def _ensure_agent(
    db: AsyncSession, aid: str, project_id: str, name: str, display_name: str, kind: str
):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(Agent).values(
        id=aid, project_id=project_id, name=name, display_name=display_name, kind=kind
    ).on_conflict_do_nothing(index_elements=["id"])
    await db.execute(stmt)
    await db.commit()

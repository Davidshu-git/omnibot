from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(ts_str: str | None) -> datetime:
    if not ts_str:
        return now()
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return now()

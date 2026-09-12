"""Small helpers shared by the service layer."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def parse_ts(s: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (with/without 'Z' or millis) into an aware UTC datetime."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """Format like db.utcnow(): 2026-09-12T08:30:00.000Z."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ago(delta: timedelta, now: datetime | None = None) -> str:
    """ISO timestamp `delta` before now (UTC)."""
    return iso((now or datetime.now(timezone.utc)) - delta)


def json_list(s: Any) -> list[str]:
    """Decode a JSON array column into a list of strings ('[]' / NULL / garbage -> [])."""
    if not s:
        return []
    try:
        v = json.loads(s) if isinstance(s, (str, bytes)) else s
    except (TypeError, ValueError):
        return []
    return [str(x) for x in v] if isinstance(v, list) else []

"""Google Takeout watch-history.json -> topic weights (keyword matching; no LLM needed)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import tx, utcnow
from app.taxonomy import match_topics, topic_ids

from .common import parse_ts

MAX_ENTRIES = 3000
MAX_AGE_DAYS = 365


def parse_watch_history(data: bytes) -> list[dict[str, Any]]:
    """[{title, channel, time}] newest first, last 12 months, at most 3000 entries."""
    try:
        items = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict) or it.get("header") not in (None, "YouTube", "YouTube Music"):
            continue
        title = str(it.get("title") or "")
        if title.startswith("Watched "):
            title = title[len("Watched "):]
        elif title.startswith("시청한 동영상: "):
            title = title[len("시청한 동영상: "):]
        if not title or title.startswith("https://"):
            continue
        ts = parse_ts(it.get("time"))
        if ts is not None and ts < cutoff:
            continue
        subs = it.get("subtitles") or []
        channel = str(subs[0].get("name", "")) if subs and isinstance(subs[0], dict) else ""
        out.append({"title": title, "channel": channel, "time": ts})
    out.sort(key=lambda e: e["time"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:MAX_ENTRIES]


def topic_weights(entries: list[dict[str, Any]]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for e in entries:
        for t in match_topics(f"{e.get('title', '')} {e.get('channel', '')}"):
            counts[t] = counts.get(t, 0) + 1
    if not counts:
        return {}
    mx = max(counts.values())
    return {t: round(c / mx, 4) for t, c in counts.items()}


def apply_weights(conn: sqlite3.Connection, weights: dict[str, float]) -> None:
    """topic_affinity.base = 0.5*current + 0.5*weight for every taxonomy topic."""
    now = utcnow()
    with tx(conn):
        for t in topic_ids():
            cur = conn.execute("SELECT base FROM topic_affinity WHERE topic=?", (t,)).fetchone()
            base = float(cur[0]) if cur else 0.0
            new = round(0.5 * base + 0.5 * float(weights.get(t, 0.0)), 4)
            conn.execute("INSERT INTO topic_affinity(topic, base, learned, updated_at) VALUES(?, ?, 0, ?) "
                         "ON CONFLICT(topic) DO UPDATE SET base=excluded.base, updated_at=excluded.updated_at", (t, new, now))

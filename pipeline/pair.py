"""Pair mode: match German videos with the same content on an English counterpart channel."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.db import tx

log = logging.getLogger("pipeline")

DURATION_TOLERANCE_S = 1
PUBLISHED_WINDOW_DAYS = 30


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_pair(de_video: dict[str, Any], en_videos: list[dict[str, Any]]) -> str | None:
    """Closest-published English video with |duration diff| <= 1 s within 30 days, else None."""
    if de_video.get("duration_s") is None:
        return None
    d_ts = _ts(de_video.get("published_at"))
    best: tuple[float, str] | None = None
    for en in en_videos:
        if en.get("duration_s") is None or abs(int(en["duration_s"]) - int(de_video["duration_s"])) > DURATION_TOLERANCE_S:
            continue
        e_ts = _ts(en.get("published_at"))
        if d_ts and e_ts:
            gap_days = abs((d_ts - e_ts).total_seconds()) / 86400
            if gap_days > PUBLISHED_WINDOW_DAYS:
                continue
        else:
            gap_days = PUBLISHED_WINDOW_DAYS  # unknown dates: acceptable but least preferred
        if best is None or gap_days < best[0]:
            best = (gap_days, en["id"])
    return best[1] if best else None


def match_pairs(conn: sqlite3.Connection) -> dict[str, int]:
    summary = {"paired": 0, "dub_flagged": 0}
    with tx(conn):
        cur = conn.execute("UPDATE videos SET has_dub=2 WHERE has_dub<2 AND channel_id IN "
                           "(SELECT id FROM channels WHERE dubs=1)")
        summary["dub_flagged"] = cur.rowcount
    channels = conn.execute("SELECT id, handle, en_counterpart_id FROM channels WHERE en_counterpart_id IS NOT NULL").fetchall()
    for ch in channels:
        en_videos = [dict(r) for r in conn.execute(
            "SELECT id, duration_s, published_at FROM videos WHERE channel_id=?", (ch["en_counterpart_id"],))]
        if not en_videos:
            continue
        de_videos = [dict(r) for r in conn.execute(
            "SELECT id, duration_s, published_at FROM videos WHERE channel_id=? AND pair_video_id IS NULL", (ch["id"],))]
        with tx(conn):
            for de in de_videos:
                pid = find_pair(de, en_videos)
                if pid:
                    conn.execute("UPDATE videos SET pair_video_id=? WHERE id=?", (pid, de["id"]))
                    summary["paired"] += 1
        log.info("pair %s: %d matched", ch["handle"], summary["paired"])
    return summary

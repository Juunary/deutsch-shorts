"""Turn user events into affinity updates (topics, channels, level bias) and nightly decay."""
from __future__ import annotations

import sqlite3
from typing import Any

from app.db import get_setting, set_setting, tx, utcnow
from app.models import EventIn

from .common import clamp, json_list

# (topic delta, channel delta) per event type
DELTAS: dict[str, tuple[float, float]] = {
    "like": (0.15, 0.20),
    "skip": (-0.05, -0.10),
    "complete": (0.05, 0.05),
}
LEVEL_BIAS_STEP = 0.05
LEVEL_BIAS_CAP = 0.5


def _video_topics(conn: sqlite3.Connection, video_id: str) -> tuple[str | None, list[str], str | None]:
    r = conn.execute("SELECT v.channel_id, v.topics_json, v.cefr, c.topics_json AS ct, c.level_hint "
                     "FROM videos v JOIN channels c ON c.id=v.channel_id WHERE v.id=?", (video_id,)).fetchone()
    if r is None:
        return None, [], None
    return r["channel_id"], (json_list(r["topics_json"]) or json_list(r["ct"])), (r["cefr"] or r["level_hint"])


def _bump_topics(conn: sqlite3.Connection, topics: list[str], delta: float) -> None:
    now = utcnow()
    for t in topics:
        conn.execute(
            "INSERT INTO topic_affinity(topic, base, learned, updated_at) VALUES(?, 0, ?, ?) "
            "ON CONFLICT(topic) DO UPDATE SET learned=MAX(-1.0, MIN(1.0, learned + excluded.learned)), updated_at=excluded.updated_at",
            (t, clamp(delta), now),
        )


def _bump_channel(conn: sqlite3.Connection, channel_id: str | None, delta: float) -> None:
    if not channel_id:
        return
    conn.execute(
        "INSERT INTO channel_affinity(channel_id, score, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET score=MAX(-1.0, MIN(1.0, score + excluded.score)), updated_at=excluded.updated_at",
        (channel_id, clamp(delta), utcnow()),
    )


def apply_event(conn: sqlite3.Connection, ev: EventIn) -> None:
    """Insert the event row and apply its side effects. Caller wraps in a transaction."""
    conn.execute("INSERT INTO events(video_id, type, value, mode, created_at) VALUES(?,?,?,?,?)",
                 (ev.video_id, ev.type, ev.value, ev.mode, utcnow()))
    kind = ev.type
    if kind == "watch" and ev.value is not None:
        if ev.value < 0.3:
            kind = "skip"
        elif ev.value >= 0.9:
            kind = "complete"
        else:
            return
    if kind in DELTAS:
        channel_id, topics, _ = _video_topics(conn, ev.video_id)
        td, cd = DELTAS[kind]
        _bump_topics(conn, topics, td)
        _bump_channel(conn, channel_id, cd)
    elif kind in ("too_hard", "too_easy"):
        _, _, cefr = _video_topics(conn, ev.video_id)
        if cefr:
            bias: dict[str, Any] = get_setting(conn, "level_bias", {}) or {}
            step = -LEVEL_BIAS_STEP if kind == "too_hard" else LEVEL_BIAS_STEP
            bias[cefr] = clamp(float(bias.get(cefr, 0.0)) + step, -LEVEL_BIAS_CAP, LEVEL_BIAS_CAP)
            set_setting(conn, "level_bias", bias)
    elif kind == "embed_error":
        conn.execute("UPDATE videos SET embeddable=0 WHERE id=?", (ev.video_id,))
    elif kind == "no_dub":
        conn.execute("UPDATE videos SET has_dub=0 WHERE id=?", (ev.video_id,))


def apply_events(conn: sqlite3.Connection, events: list[EventIn]) -> int:
    with tx(conn):
        for ev in events:
            apply_event(conn, ev)
    return len(events)


def decay(conn: sqlite3.Connection, factor: float = 0.98) -> dict[str, int]:
    with tx(conn):
        a = conn.execute("UPDATE topic_affinity SET learned=learned*?, updated_at=? WHERE learned != 0", (factor, utcnow())).rowcount
        b = conn.execute("UPDATE channel_affinity SET score=score*?, updated_at=? WHERE score != 0", (factor, utcnow())).rowcount
    return {"topics": a, "channels": b}

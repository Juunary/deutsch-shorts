"""Feed scoring: level fit, topic affinity, novelty, recency, channel affinity + diversity re-ranking."""
from __future__ import annotations

import json
import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.db import all_settings
from app.models import ChannelBrief, FeedItem

from .common import clamp, json_list, parse_ts

BANDS_NORMAL = {"A1": 1.0, "A2": 1.0, "B1": 0.3, "B2": 0.05, "C1": 0.0}
BANDS_STRETCH = {"A1": 0.6, "A2": 1.0, "B1": 0.9, "B2": 0.3, "C1": 0.05}
WEIGHTS = {"level": 0.35, "level_stretch": 0.25, "topic": 0.30, "novelty": 0.15, "recency": 0.10, "channel": 0.10}
CANDIDATE_LIMIT = 5000
EXPLORATION_SLOTS = (5, 10)          # 1-based positions filled from the exploration pool
EXPLORATION_RANKS = (50, 300)        # pool = remaining candidates ranked 50..300
CHANNEL_REPEAT_PENALTY = 0.6


@dataclass
class Ctx:
    stretch: bool = False
    user_topics: dict[str, float] = field(default_factory=dict)
    channel_aff: dict[str, float] = field(default_factory=dict)
    prefer_dub: bool = False
    llm_on: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---- components -------------------------------------------------------------
def level_fit(cefr: str | None, ctx: Ctx) -> float:
    bands = BANDS_STRETCH if ctx.stretch else BANDS_NORMAL
    return bands.get(cefr or "", 0.3)


def topic_score(topics: Iterable[str], user_topics: dict[str, float]) -> float:
    ts = list(topics)
    if not ts:
        return 0.0
    return sum(clamp(float(user_topics.get(t, 0.0)), 0.0, 1.0) for t in ts) / len(ts)


def novelty(v: dict[str, Any], ctx: Ctx) -> float:
    completed_at = v.get("completed_at")
    if completed_at is not None and (ctx.now - completed_at) <= timedelta(days=60):
        return 0.0
    watched_at, watched_max = v.get("watched_at"), v.get("watched_max")
    if watched_at is not None and watched_max is not None and watched_max >= 0.5 and (ctx.now - watched_at) <= timedelta(days=60):
        return 0.0
    last_imp = v.get("last_impression")
    if last_imp is not None:
        return 0.4 if (ctx.now - last_imp) > timedelta(days=3) else 0.1
    return 1.0


def recency(published_at: datetime | None, now: datetime) -> float:
    if published_at is None:
        return 0.5
    age_days = max(0.0, (now - published_at).total_seconds() / 86400.0)
    return math.exp(-age_days / 30.0)


def score(v: dict[str, Any], ctx: Ctx) -> float:
    w_level = WEIGHTS["level_stretch"] if ctx.stretch else WEIGHTS["level"]
    s = (w_level * level_fit(v.get("cefr"), ctx)
         + WEIGHTS["topic"] * topic_score(v.get("topics") or [], ctx.user_topics)
         + WEIGHTS["novelty"] * novelty(v, ctx)
         + WEIGHTS["recency"] * recency(v.get("published_dt"), ctx.now)
         + WEIGHTS["channel"] * (clamp(float(ctx.channel_aff.get(v.get("channel_id", ""), 0.0))) + 1.0) / 2.0)
    if ctx.prefer_dub and int(v.get("has_dub") or 0) > 0:
        s += 0.05
    if ctx.llm_on and v.get("enrich_status") != "ok":
        s -= 0.05
    if (v.get("duration_s") or 0) > 90:
        s -= 0.10
    return s


def rerank_diverse(scored: list[tuple[float, dict[str, Any]]], n: int, rng: random.Random | None = None) -> list[dict[str, Any]]:
    """Greedy pick with a per-channel repeat penalty and two exploration slots."""
    rng = rng or random.Random()
    remaining = sorted(scored, key=lambda p: p[0], reverse=True)
    picks: list[dict[str, Any]] = []
    channel_count: dict[str, int] = {}
    while remaining and len(picks) < n:
        slot = len(picks) + 1
        chosen_idx: int | None = None
        if slot in EXPLORATION_SLOTS:
            lo, hi = EXPLORATION_RANKS
            if len(remaining) > lo:
                chosen_idx = rng.randrange(lo - 1, min(hi, len(remaining)))
        if chosen_idx is None:
            best_val = -1e9
            for i, (s, v) in enumerate(remaining):
                eff = s * (CHANNEL_REPEAT_PENALTY ** channel_count.get(v.get("channel_id", ""), 0))
                if eff > best_val:
                    best_val, chosen_idx = eff, i
        s, v = remaining.pop(chosen_idx)
        channel_count[v.get("channel_id", "")] = channel_count.get(v.get("channel_id", ""), 0) + 1
        picks.append(v)
    return picks


# ---- assembly ---------------------------------------------------------------
def _event_state(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per video: last impression, last/most watched, completed."""
    state: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT video_id, type, MAX(created_at) AS last_at, MAX(value) AS max_value "
                          "FROM events WHERE type IN ('impression','watch','complete') GROUP BY video_id, type"):
        st = state.setdefault(r["video_id"], {})
        last = parse_ts(r["last_at"])
        if r["type"] == "impression":
            st["last_impression"] = last
        elif r["type"] == "watch":
            st["watched_at"] = last
            st["watched_max"] = float(r["max_value"]) if r["max_value"] is not None else None
        elif r["type"] == "complete":
            st["completed_at"] = last
    return state


def load_context(conn: sqlite3.Connection) -> Ctx:
    s = all_settings(conn)
    user_topics = {r["topic"]: clamp(float(r["base"]) + float(r["learned"]), 0.0, 1.0)
                   for r in conn.execute("SELECT topic, base, learned FROM topic_affinity")}
    channel_aff = {r["channel_id"]: float(r["score"]) for r in conn.execute("SELECT channel_id, score FROM channel_affinity")}
    from app.config import settings as cfg
    return Ctx(stretch=bool(s.get("stretch")), user_topics=user_topics, channel_aff=channel_aff,
               prefer_dub=bool(s.get("prefer_dub")), llm_on=bool(cfg.llm_enabled and s.get("llm_enabled", True)))


def candidates(conn: sqlite3.Connection, exclude: set[str], n: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT v.id, v.channel_id, v.title, v.duration_s, v.cefr, v.topics_json, v.published_at, v.has_dub, "
        "v.pair_video_id, v.summary_ko, v.enrich_status, c.title AS channel_title, c.handle AS channel_handle, "
        "c.level_hint, c.topics_json AS channel_topics "
        "FROM videos v JOIN channels c ON c.id=v.channel_id "
        "WHERE v.transcript_status='ok' AND v.embeddable=1 AND v.region_blocked=0 AND c.enabled=1 "
        "ORDER BY v.published_at DESC LIMIT ?", (CANDIDATE_LIMIT,)).fetchall()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    recent = {r[0] for r in conn.execute("SELECT DISTINCT video_id FROM events WHERE type='impression' AND created_at >= ?", (since,))}
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["id"] in exclude:
            continue
        topics = json_list(r["topics_json"]) or json_list(r["channel_topics"])
        out.append({
            "id": r["id"], "channel_id": r["channel_id"], "title": r["title"], "duration_s": r["duration_s"],
            "cefr": r["cefr"] or r["level_hint"] or "B1", "topics": topics,
            "published_at": r["published_at"], "published_dt": parse_ts(r["published_at"]),
            "has_dub": r["has_dub"], "pair_video_id": r["pair_video_id"], "summary_ko": r["summary_ko"],
            "enrich_status": r["enrich_status"], "channel_title": r["channel_title"], "channel_handle": r["channel_handle"],
            "recent_impression": r["id"] in recent,
        })
    fresh = [v for v in out if not v["recent_impression"]]
    return fresh if len(fresh) >= n else out


def build_feed(conn: sqlite3.Connection, n: int = 10, exclude: Iterable[str] = (), rng: random.Random | None = None) -> list[FeedItem]:
    ctx = load_context(conn)
    cands = candidates(conn, set(exclude), n)
    state = _event_state(conn)
    scored: list[tuple[float, dict[str, Any]]] = []
    for v in cands:
        v.update(state.get(v["id"], {}))
        scored.append((score(v, ctx), v))
    picks = rerank_diverse(scored, n, rng)
    return [FeedItem(
        video_id=v["id"], channel=ChannelBrief(id=v["channel_id"], title=v["channel_title"], handle=v["channel_handle"]),
        title=v["title"], duration_s=v["duration_s"], cefr=v["cefr"], topics=v["topics"], has_dub=int(v["has_dub"] or 0),
        pair_video_id=v["pair_video_id"], summary_ko=v["summary_ko"], has_model=(v["enrich_status"] == "ok"),
        youtube_url=f"https://www.youtube.com/shorts/{v['id']}",
    ) for v in picks]

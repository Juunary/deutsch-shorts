"""Transcript fetching (youtube-transcript-api), segment merging and machine-translation fallback.

YouTube temporarily blocks IPs that fetch captions in bursts (observed after ~12 requests in a few
minutes from the home connection), so this stage is deliberately slow: one video per
TRANSCRIPT_SLEEP_MIN..MAX seconds, and a run-level cooldown that doubles on consecutive blocks.
"""
from __future__ import annotations

import html
import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.config import settings
from app.db import get_setting, set_setting, tx, utcnow

from .translate import TranslateError, translate_batch

log = logging.getLogger("pipeline")

MAX_WORDS = 12
MAX_GAP_S = 0.7
MAX_SPAN_S = 7.0
MIN_SEGMENT_MS = 500
MAX_ATTEMPTS = 5
NO_GERMAN_CHANNEL_THRESHOLD = 5   # channels with >= N videos lacking a German track and none with one are skipped
LEVEL_ORDER_SQL = "CASE c.level_hint WHEN 'A1' THEN 0 WHEN 'A2' THEN 1 WHEN 'B1' THEN 2 WHEN 'B2' THEN 3 ELSE 4 END"
MT_SOURCES = ("deepl", "gtx", "yt_mt")

_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", html.unescape(text or "").replace("\n", " ")).strip()


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_de(video_id: str, api: Any = None) -> dict[str, Any]:
    """Fetch the German transcript (manual preferred, else auto-generated).

    Returns {"status": ok|none|disabled|unavailable|blocked|error, "lang", "is_generated", "snippets", "error"}.
    """
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        AgeRestricted, CouldNotRetrieveTranscript, InvalidVideoId, NoTranscriptFound, PoTokenRequired,
        RequestBlocked, TranscriptsDisabled, VideoUnavailable, VideoUnplayable,
    )

    api = api or YouTubeTranscriptApi()
    try:
        tl = api.list(video_id)
    except (RequestBlocked, PoTokenRequired) as e:  # IpBlocked subclasses RequestBlocked
        return {"status": "blocked", "error": type(e).__name__}
    except TranscriptsDisabled:
        return {"status": "disabled"}
    except (VideoUnavailable, VideoUnplayable, AgeRestricted, InvalidVideoId) as e:
        return {"status": "unavailable", "error": type(e).__name__}
    except CouldNotRetrieveTranscript as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}
    except Exception as e:  # network etc.
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}

    transcript = None
    for finder in (tl.find_manually_created_transcript, tl.find_generated_transcript):
        try:
            transcript = finder(["de"])
            break
        except NoTranscriptFound:
            continue
    if transcript is None:
        langs = sorted({t.language_code for t in tl})
        return {"status": "none", "error": f"no de track; available: {langs}"}
    try:
        snippets = transcript.fetch().to_raw_data()
    except (RequestBlocked, PoTokenRequired) as e:
        return {"status": "blocked", "error": type(e).__name__}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"status": "ok", "lang": transcript.language_code, "is_generated": bool(transcript.is_generated),
            "snippets": snippets}


# ---------------------------------------------------------------------------
# Merging (pure)
# ---------------------------------------------------------------------------
def group_snippets(snippets: list[dict[str, Any]]) -> list[list[int]]:
    """Indices of snippets to merge into one segment each (word cap, small gaps, short spans)."""
    groups: list[list[int]] = []
    cur: list[int] = []
    cur_words = 0
    cur_start = 0.0
    prev_end = 0.0
    for i, s in enumerate(snippets):
        text = _clean(s.get("text", ""))
        if not text:
            continue
        start = float(s.get("start", 0.0))
        dur = float(s.get("duration", 0.0) or 0.0)
        words = len(text.split())
        if cur:
            gap = start - prev_end
            span = (start + dur) - cur_start
            if cur_words + words <= MAX_WORDS and gap < MAX_GAP_S and span <= MAX_SPAN_S:
                cur.append(i)
                cur_words += words
                prev_end = max(prev_end, start + dur)
                continue
            groups.append(cur)
        cur = [i]
        cur_words = words
        cur_start = start
        prev_end = start + dur
    if cur:
        groups.append(cur)
    return groups


def merge_by_groups(snippets: list[dict[str, Any]], groups: list[list[int]]) -> list[dict[str, Any]]:
    """Build segments [{idx, start_ms, end_ms, text}] from snippet index groups."""
    segs: list[dict[str, Any]] = []
    for idx, g in enumerate(groups):
        first, last = snippets[g[0]], snippets[g[-1]]
        start_ms = int(round(float(first.get("start", 0.0)) * 1000))
        natural_end = int(round((float(last.get("start", 0.0)) + float(last.get("duration", 0.0) or 0.0)) * 1000))
        text = " ".join(_clean(snippets[i].get("text", "")) for i in g).strip()
        segs.append({"idx": idx, "start_ms": start_ms, "end_ms": natural_end, "text": text})
    for a, b in zip(segs, segs[1:]):
        a["end_ms"] = min(a["end_ms"], b["start_ms"])
    for s in segs:
        if s["end_ms"] < s["start_ms"] + MIN_SEGMENT_MS:
            s["end_ms"] = s["start_ms"] + MIN_SEGMENT_MS
    return segs


def merge_segments(snippets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return merge_by_groups(snippets, group_snippets(snippets))


def lookup(segments: list[dict[str, Any]], t_ms: int) -> int:
    """Index of the segment active at t_ms (last segment whose start <= t), -1 before the first."""
    lo, hi, ans = 0, len(segments) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if segments[mid]["start_ms"] <= t_ms:
            ans, lo = mid, mid + 1
        else:
            hi = mid - 1
    return ans


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def store_transcript(conn: sqlite3.Connection, video_id: str, result: dict[str, Any]) -> int:
    segs = merge_segments(result["snippets"])
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO transcript_raw(video_id, lang, is_generated, json, fetched_at) "
                     "VALUES(?,?,?,?,?)",
                     (video_id, result.get("lang", "de"), 1 if result.get("is_generated", True) else 0,
                      json.dumps(result["snippets"], ensure_ascii=False), utcnow()))
        conn.execute("DELETE FROM segments WHERE video_id=?", (video_id,))
        conn.executemany("INSERT INTO segments(video_id, idx, start_ms, end_ms, text_de) VALUES(?,?,?,?,?)",
                         [(video_id, s["idx"], s["start_ms"], s["end_ms"], s["text"]) for s in segs])
        conn.execute("UPDATE videos SET transcript_status=?, transcript_attempts=0, next_transcript_try_at=NULL "
                     "WHERE id=?", ("ok" if segs else "none", video_id))
    return len(segs)


def translate_video(conn: sqlite3.Connection, video_id: str, langs: tuple[str, ...] = ("ko", "en")) -> dict[str, str]:
    """Fill machine translations for languages that have no translation rows yet."""
    texts = [r[0] for r in conn.execute("SELECT text_de FROM segments WHERE video_id=? ORDER BY idx", (video_id,))]
    done: dict[str, str] = {}
    if not texts:
        return done
    for lang in langs:
        exists = conn.execute("SELECT 1 FROM translations WHERE video_id=? AND lang=? LIMIT 1", (video_id, lang)).fetchone()
        if exists:
            continue
        provider, out = translate_batch(texts, lang)
        with tx(conn):
            conn.executemany("INSERT OR REPLACE INTO translations(video_id, idx, lang, source, text) VALUES(?,?,?,?,?)",
                             [(video_id, i, lang, provider, t) for i, t in enumerate(out) if t])
        done[lang] = provider
    return done


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def _cooldown_active(conn: sqlite3.Connection) -> str | None:
    until = get_setting(conn, "transcript_cooldown_until")
    if until and until > utcnow():
        return until
    return None


def _set_block_cooldown(conn: sqlite3.Connection) -> str:
    streak = int(get_setting(conn, "transcript_block_streak", 0) or 0) + 1
    hours = 2 * (2 ** (streak - 1))
    until = (datetime.now(timezone.utc) + timedelta(hours=min(hours, 48))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with tx(conn):
        set_setting(conn, "transcript_block_streak", streak)
        set_setting(conn, "transcript_cooldown_until", until)
    return until


def candidates(conn: sqlite3.Connection, limit: int) -> list[str]:
    now = utcnow()
    sql = (
        "SELECT v.id FROM videos v JOIN channels c ON c.id=v.channel_id "
        "WHERE c.enabled=1 AND v.embeddable=1 AND (v.transcript_status='pending' OR "
        "(v.transcript_status='failed' AND v.transcript_attempts < ? AND "
        "(v.next_transcript_try_at IS NULL OR v.next_transcript_try_at <= ?))) "
        "AND v.channel_id NOT IN (SELECT channel_id FROM videos GROUP BY channel_id "
        "HAVING SUM(transcript_status IN ('none','mixed')) >= ? AND SUM(transcript_status='ok') = 0) "
        f"ORDER BY {LEVEL_ORDER_SQL}, v.published_at DESC LIMIT ?"
    )
    return [r[0] for r in conn.execute(sql, (MAX_ATTEMPTS, now, NO_GERMAN_CHANNEL_THRESHOLD, limit))]


def run_transcripts(conn: sqlite3.Connection, limit: int = 30, video_id: str | None = None,
                    translate: bool = True, fetcher: Callable[[str], dict[str, Any]] = fetch_de,
                    sleep_range: tuple[float, float] | None = None, sleep_fn: Callable[[float], None] = time.sleep,
                    ) -> dict[str, Any]:
    summary: dict[str, Any] = {"ok": 0, "none": 0, "failed": 0, "blocked": False, "translated": 0, "skipped_cooldown": False}
    until = _cooldown_active(conn)
    if until and not video_id:
        log.warning("transcripts: cooldown active until %s, skipping", until)
        summary["skipped_cooldown"] = True
        return summary
    ids = [video_id] if video_id else candidates(conn, limit)
    lo, hi = sleep_range or (settings.transcript_sleep_min, settings.transcript_sleep_max)
    for n, vid in enumerate(ids):
        result = fetcher(vid)
        status = result.get("status")
        if status == "ok":
            count = store_transcript(conn, vid, result)
            summary["ok"] += 1
            log.info("transcript %s: %d segments (%s)", vid, count, "auto" if result.get("is_generated") else "manual")
            if translate and count:
                try:
                    done = translate_video(conn, vid)
                    summary["translated"] += len(done)
                except TranslateError as e:
                    log.warning("translate %s: %s", vid, e)
            with tx(conn):
                set_setting(conn, "transcript_block_streak", 0)
        elif status == "blocked":
            until = _set_block_cooldown(conn)
            summary["blocked"] = True
            log.error("transcripts: %s from YouTube, cooling down until %s", result.get("error"), until)
            break
        elif status in ("none", "disabled"):
            with tx(conn):
                conn.execute("UPDATE videos SET transcript_status=? WHERE id=?", (status, vid))
            summary["none"] += 1
            log.info("transcript %s: %s %s", vid, status, result.get("error", ""))
        elif status == "unavailable":
            with tx(conn):
                conn.execute("UPDATE videos SET transcript_status='none', embeddable=0 WHERE id=?", (vid,))
            summary["none"] += 1
        else:
            attempts = int(conn.execute("SELECT transcript_attempts FROM videos WHERE id=?", (vid,)).fetchone()[0]) + 1
            next_try = (datetime.now(timezone.utc) + timedelta(hours=2 ** attempts)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            with tx(conn):
                conn.execute("UPDATE videos SET transcript_status='failed', transcript_attempts=?, "
                             "next_transcript_try_at=? WHERE id=?", (attempts, next_try, vid))
            summary["failed"] += 1
            log.warning("transcript %s failed (%d/%d): %s", vid, attempts, MAX_ATTEMPTS, result.get("error"))
        if n < len(ids) - 1:
            sleep_fn(random.uniform(lo, hi))
    return summary


def run_translate(conn: sqlite3.Connection, limit: int = 200) -> dict[str, Any]:
    """Fill missing machine translations for videos with transcripts (separate from the YouTube loop)."""
    summary: dict[str, Any] = {"videos": 0, "translated": 0, "errors": 0}
    ids = [r[0] for r in conn.execute(
        "SELECT v.id FROM videos v WHERE v.transcript_status='ok' AND ("
        "NOT EXISTS (SELECT 1 FROM translations t WHERE t.video_id=v.id AND t.lang='ko') OR "
        "NOT EXISTS (SELECT 1 FROM translations t WHERE t.video_id=v.id AND t.lang='en')) "
        "ORDER BY v.published_at DESC LIMIT ?", (limit,))]
    for vid in ids:
        try:
            done = translate_video(conn, vid)
        except TranslateError as e:
            summary["errors"] += 1
            log.warning("translate %s: %s", vid, e)
            if "429" in str(e) or "rate" in str(e).lower():
                break
            continue
        summary["videos"] += 1
        summary["translated"] += len(done)
    return summary

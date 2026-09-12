"""Channel seeding and Shorts discovery.

Two discovery paths:
* Data API (needs YOUTUBE_API_KEY): UUSH... Shorts playlist -> playlistItems.list -> videos.list
  (1 unit each; ~65 units/day for 60 channels once backfilled).
* RSS (no key): https://www.youtube.com/feeds/videos.xml?playlist_id=UUSH... gives the 15 newest
  Shorts with title/published date; details (duration, embeddable) are filled by `backfill_details`
  once a key exists.
"""
from __future__ import annotations

import logging
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.config import ROOT, settings
from app.db import tx, utcnow

from .rss import FeedError, fetch_playlist_feed
from .youtube_api import (
    PlaylistNotFound,
    QuotaExceeded,
    YouTubeAPI,
    YouTubeAPIError,
    is_dub_description,
    is_region_blocked,
    parse_iso8601_duration,
    uploads_id,
    uush_id,
)

log = logging.getLogger("pipeline")

CHANNELS_FILE = ROOT / "data" / "channels.yaml"
MIN_DURATION_S = 10
MAX_DURATION_S = 180
BACKFILL_PAGES = 4
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
# Without these cookies youtube.com answers with the consent interstitial (consent.youtube.com) instead of the page.
CONSENT_COOKIES = {"SOCS": "CAI", "CONSENT": "YES+cb.20210328-17-p0.en+FX+417"}
_CHANNEL_ID_RE = re.compile(r'channel_id=(UC[\w-]{22})')
_EXTERNAL_ID_RE = re.compile(r'"externalId":"(UC[\w-]{22})"')


# ---------------------------------------------------------------------------
# Seed file
# ---------------------------------------------------------------------------
def load_seed(path: Path = CHANNELS_FILE) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[dict[str, Any]] = []
    for e in data.get("channels") or []:
        if not isinstance(e, dict):
            continue
        handle = str(e.get("handle") or "").strip().lstrip("@")
        cid = str(e.get("id") or "").strip() or None
        if not handle and not cid:
            continue
        out.append({
            "handle": handle or None,
            "id": cid,
            "level_hint": (e.get("level_hint") or None),
            "topics": [str(t) for t in (e.get("topics") or [])],
            "dubs": 1 if e.get("dubs") else 0,
            "en_counterpart": (str(e.get("en_counterpart") or "").strip().lstrip("@") or None),
            "title": e.get("title"),
        })
    return out


def resolve_handle_html(handle: str, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """Key-less fallback: read the channel id from the public channel page (RSS link tag)."""
    # A fresh client per attempt: responses set their own CONSENT cookie in a shared jar, which then
    # overrides ours and brings the consent interstitial back.
    r = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True, cookies=dict(CONSENT_COOKIES),
                              headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"}) as c:
                r = c.get(f"https://www.youtube.com/@{handle}")
        except httpx.HTTPError as e:
            log.warning("resolve_handle_html(%s): %s", handle, e)
            return None
        if r.status_code == 200 and "consent.youtube.com" not in str(r.url):
            break
        time.sleep(1.5)
    if r is None or r.status_code != 200 or "consent.youtube.com" in str(r.url):
        log.warning("resolve_handle_html(%s): HTTP %s at %s", handle, getattr(r, "status_code", "?"), getattr(r, "url", "?"))
        return None
    m = _CHANNEL_ID_RE.search(r.text) or _EXTERNAL_ID_RE.search(r.text)
    if not m:
        return None
    title = None
    tm = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
    if tm:
        title = tm.group(1)
    return {"id": m.group(1), "title": title, "uploads_playlist_id": uploads_id(m.group(1))}


def _existing_id_for_handle(conn: sqlite3.Connection, handle: str) -> str | None:
    r = conn.execute("SELECT id FROM channels WHERE lower(handle)=lower(?)", (handle,)).fetchone()
    return r[0] if r else None


def upsert_channel(conn: sqlite3.Connection, ch: dict[str, Any], enabled: int | None = 1) -> None:
    """Insert or update a channel row; `enabled` is only applied on insert."""
    conn.execute(
        "INSERT INTO channels(id, handle, title, shorts_playlist_id, level_hint, topics_json, dubs, "
        "en_counterpart_id, enabled) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "handle=COALESCE(excluded.handle, channels.handle), "
        "title=COALESCE(excluded.title, channels.title), "
        "shorts_playlist_id=excluded.shorts_playlist_id, "
        "level_hint=COALESCE(excluded.level_hint, channels.level_hint), "
        "topics_json=CASE WHEN excluded.topics_json='[]' THEN channels.topics_json ELSE excluded.topics_json END, "
        "dubs=MAX(excluded.dubs, channels.dubs), "
        "en_counterpart_id=COALESCE(excluded.en_counterpart_id, channels.en_counterpart_id)",
        (
            ch["id"], ch.get("handle"), ch.get("title"), uush_id(ch["id"]), ch.get("level_hint"),
            json.dumps(ch.get("topics") or [], ensure_ascii=False), int(ch.get("dubs") or 0),
            ch.get("en_counterpart_id"), 1 if enabled is None else int(enabled),
        ),
    )


def seed(conn: sqlite3.Connection, api: YouTubeAPI | None, entries: list[dict[str, Any]] | None = None,
         html_fallback: bool = True, sleep: float = 2.0) -> dict[str, Any]:
    """Upsert data/channels.yaml into `channels`, resolving handles to ids (API, cached, or HTML fallback)."""
    entries = load_seed() if entries is None else entries
    summary: dict[str, Any] = {"resolved": 0, "unresolved": [], "counterparts": 0}
    use_api = api is not None and bool(api.api_key)
    html_client = httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": BROWSER_UA})

    def resolve(handle: str | None, cid: str | None) -> dict[str, Any] | None:
        if cid:
            return {"id": cid, "title": None}
        if not handle:
            return None
        existing = _existing_id_for_handle(conn, handle)
        if existing:
            return {"id": existing, "title": None}
        if use_api:
            try:
                return api.resolve_handle(handle)
            except (QuotaExceeded, YouTubeAPIError) as e:
                log.warning("resolve %s via API failed: %s", handle, e)
        if html_fallback:
            info = resolve_handle_html(handle, html_client)
            time.sleep(sleep)
            return info
        return None

    try:
        for e in entries:
            info = resolve(e.get("handle"), e.get("id"))
            if not info:
                summary["unresolved"].append(e.get("handle") or e.get("id"))
                log.warning("seed: could not resolve %s", e.get("handle") or e.get("id"))
                continue
            counterpart_id = None
            if e.get("en_counterpart"):
                cp = resolve(e["en_counterpart"], None)
                if cp:
                    counterpart_id = cp["id"]
                    with tx(conn):
                        upsert_channel(conn, {
                            "id": cp["id"], "handle": e["en_counterpart"], "title": cp.get("title"),
                            "level_hint": None, "topics": e.get("topics") or [], "dubs": 0,
                        }, enabled=0)
                    summary["counterparts"] += 1
                else:
                    log.warning("seed: counterpart %s unresolved", e["en_counterpart"])
            with tx(conn):
                upsert_channel(conn, {
                    "id": info["id"], "handle": e.get("handle"), "title": e.get("title") or info.get("title"),
                    "level_hint": e.get("level_hint"), "topics": e.get("topics") or [], "dubs": e.get("dubs", 0),
                    "en_counterpart_id": counterpart_id,
                }, enabled=1)
            summary["resolved"] += 1
    finally:
        html_client.close()
    return summary


# ---------------------------------------------------------------------------
# Video rows
# ---------------------------------------------------------------------------
def video_row(item: dict[str, Any], channel: dict[str, Any]) -> dict[str, Any] | None:
    """videos.list item -> row dict, or None when the video must not enter the feed."""
    sn = item.get("snippet") or {}
    cd = item.get("contentDetails") or {}
    st = item.get("status") or {}
    if st.get("privacyStatus", "public") != "public":
        return None
    if st.get("embeddable") is False:
        return None
    if is_region_blocked(cd, settings.region):
        return None
    try:
        duration = parse_iso8601_duration(cd.get("duration") or "")
    except ValueError:
        return None
    if not (MIN_DURATION_S <= duration <= MAX_DURATION_S):
        return None
    desc = sn.get("description") or ""
    has_dub = 2 if int(channel.get("dubs") or 0) else (1 if is_dub_description(desc) else 0)
    return {
        "id": item["id"],
        "channel_id": channel["id"],
        "title": sn.get("title"),
        "description": desc,
        "published_at": sn.get("publishedAt"),
        "duration_s": duration,
        "default_audio_lang": sn.get("defaultAudioLanguage"),
        "embeddable": 1,
        "region_blocked": 0,
        "has_dub": has_dub,
    }


def insert_or_update_video(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert a new video or refresh metadata of an existing one. Never resets statuses."""
    conn.execute(
        "INSERT INTO videos(id, channel_id, title, description, published_at, duration_s, default_audio_lang, "
        "embeddable, region_blocked, has_dub) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description, "
        "published_at=COALESCE(excluded.published_at, videos.published_at), duration_s=excluded.duration_s, "
        "default_audio_lang=COALESCE(excluded.default_audio_lang, videos.default_audio_lang), "
        "has_dub=MAX(excluded.has_dub, videos.has_dub)",
        (row["id"], row["channel_id"], row.get("title"), row.get("description"), row.get("published_at"),
         row.get("duration_s"), row.get("default_audio_lang"), row.get("embeddable", 1),
         row.get("region_blocked", 0), row.get("has_dub", 0)),
    )


# ---------------------------------------------------------------------------
# Shorts check (fallback for channels without a UUSH playlist)
# ---------------------------------------------------------------------------
class ShortsChecker:
    """HEAD https://www.youtube.com/shorts/<id>: 200 = Short, redirect = regular video."""

    def __init__(self, client: httpx.Client | None = None, sleep: float = 1.0) -> None:
        self.client = client or httpx.Client(timeout=15.0, follow_redirects=False, headers={"User-Agent": BROWSER_UA})
        self.cache: dict[str, bool] = {}
        self.sleep = sleep

    def is_short(self, video_id: str) -> bool:
        if video_id in self.cache:
            return self.cache[video_id]
        try:
            r = self.client.head(f"https://www.youtube.com/shorts/{video_id}")
            ok = r.status_code == 200
        except httpx.HTTPError:
            ok = False
        self.cache[video_id] = ok
        time.sleep(self.sleep)
        return ok


# ---------------------------------------------------------------------------
# Ingest (API path)
# ---------------------------------------------------------------------------
def _channels(conn: sqlite3.Connection, only_enabled: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM channels WHERE shorts_playlist_id IS NOT NULL"
    if only_enabled:
        sql += " AND enabled=1"
    return [dict(r) for r in conn.execute(sql + " ORDER BY enabled DESC, handle")]


def _page_new_items(api: YouTubeAPI, playlist_id: str, known: set[str], stop_at_known: bool,
                    max_pages: int, keep=None) -> list[dict[str, Any]]:
    new: list[dict[str, Any]] = []
    token: str | None = None
    page = 0
    while True:
        items, token = api.list_playlist_items(playlist_id, token)
        page += 1
        stop = False
        for it in items:
            if it["video_id"] in known:
                if stop_at_known:
                    stop = True
                    break
                continue
            if keep is not None and not keep(it["video_id"]):
                continue
            new.append(it)
        if stop or not token or page >= max_pages:
            break
    return new


def ingest(conn: sqlite3.Connection, api: YouTubeAPI, full: bool = False, limit_channels: int | None = None,
           checker: ShortsChecker | None = None) -> dict[str, Any]:
    """Discover new Shorts for every channel (enabled ones and pair counterparts) and store details."""
    summary: dict[str, Any] = {"channels": 0, "new": 0, "filtered": 0, "errors": []}
    channels = _channels(conn)
    if limit_channels:
        channels = channels[:limit_channels]
    for ch in channels:
        known = {r[0] for r in conn.execute("SELECT id FROM videos WHERE channel_id=?", (ch["id"],))}
        first = ch.get("last_ingested_at") is None
        stop_at_known = not (first or full)
        max_pages = BACKFILL_PAGES if (first or full) else 50
        try:
            try:
                new_items = _page_new_items(api, ch["shorts_playlist_id"], known, stop_at_known, max_pages)
            except PlaylistNotFound:
                log.info("%s: no UUSH playlist, falling back to uploads + shorts check", ch.get("handle"))
                checker = checker or ShortsChecker()
                new_items = _page_new_items(api, uploads_id(ch["id"]), known, stop_at_known, BACKFILL_PAGES,
                                            keep=checker.is_short)
            details = api.videos_details([it["video_id"] for it in new_items]) if new_items else []
        except QuotaExceeded as e:
            log.error("quota exceeded, stopping ingest: %s", e)
            summary["errors"].append(str(e))
            break
        except YouTubeAPIError as e:
            log.warning("%s: %s", ch.get("handle"), e)
            summary["errors"].append(f"{ch.get('handle')}: {e}")
            continue
        inserted = 0
        filtered = 0
        with tx(conn):
            for item in details:
                row = video_row(item, ch)
                if row is None:
                    filtered += 1
                    continue
                if conn.execute("SELECT 1 FROM videos WHERE id=?", (row["id"],)).fetchone() is None:
                    inserted += 1
                insert_or_update_video(conn, row)
            conn.execute("UPDATE channels SET last_ingested_at=? WHERE id=?", (utcnow(), ch["id"]))
        summary["channels"] += 1
        summary["new"] += inserted
        summary["filtered"] += filtered
        log.info("%s: +%d new, %d filtered (units today %d)", ch.get("handle"), inserted, filtered, api.units_today())
    return summary


# ---------------------------------------------------------------------------
# Ingest (RSS path, no key) + details backfill
# ---------------------------------------------------------------------------
def ingest_rss(conn: sqlite3.Connection, sleep: float = 1.5, limit_channels: int | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"channels": 0, "new": 0, "errors": []}
    channels = _channels(conn)
    if limit_channels:
        channels = channels[:limit_channels]
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": BROWSER_UA}) as client:
        for ch in channels:
            try:
                entries = fetch_playlist_feed(ch["shorts_playlist_id"], client)
            except FeedError as e:
                log.warning("rss %s: %s", ch.get("handle"), e)
                summary["errors"].append(str(e))
                continue
            inserted = 0
            with tx(conn):
                for e in entries:
                    if conn.execute("SELECT 1 FROM videos WHERE id=?", (e["video_id"],)).fetchone():
                        continue
                    conn.execute(
                        "INSERT INTO videos(id, channel_id, title, published_at, has_dub) VALUES(?,?,?,?,?)",
                        (e["video_id"], ch["id"], e.get("title"), e.get("published_at"),
                         2 if int(ch.get("dubs") or 0) else 0),
                    )
                    inserted += 1
                conn.execute("UPDATE channels SET last_ingested_at=COALESCE(last_ingested_at, ?) WHERE id=?",
                             (utcnow(), ch["id"]))
            summary["channels"] += 1
            summary["new"] += inserted
            log.info("rss %s: +%d new", ch.get("handle"), inserted)
            time.sleep(sleep)
    return summary


def backfill_details(conn: sqlite3.Connection, api: YouTubeAPI, limit: int = 500) -> dict[str, Any]:
    """Fill duration/description/embeddable for RSS-discovered videos; drop the ones that fail the filters."""
    ids = [r[0] for r in conn.execute("SELECT id FROM videos WHERE duration_s IS NULL LIMIT ?", (limit,))]
    summary: dict[str, Any] = {"checked": len(ids), "updated": 0, "dropped": 0}
    if not ids:
        return summary
    channels = {c["id"]: c for c in _channels(conn)}
    try:
        details = api.videos_details(ids)
    except (QuotaExceeded, YouTubeAPIError) as e:
        log.error("backfill: %s", e)
        summary["error"] = str(e)
        return summary
    seen: set[str] = set()
    with tx(conn):
        for item in details:
            seen.add(item["id"])
            ch_id = conn.execute("SELECT channel_id FROM videos WHERE id=?", (item["id"],)).fetchone()[0]
            row = video_row(item, channels.get(ch_id, {"id": ch_id}))
            if row is None:
                conn.execute("DELETE FROM videos WHERE id=?", (item["id"],))
                summary["dropped"] += 1
                continue
            insert_or_update_video(conn, row)
            summary["updated"] += 1
        for vid in ids:
            if vid not in seen:  # deleted/private videos are absent from videos.list
                conn.execute("DELETE FROM videos WHERE id=?", (vid,))
                summary["dropped"] += 1
    return summary

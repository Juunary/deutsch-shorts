"""Key-less discovery through YouTube's Atom feeds (no quota, ~15 newest entries per feed)."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

try:  # pure-python, installed as a youtube-transcript-api dependency
    from defusedxml.ElementTree import fromstring as _fromstring
except ImportError:  # pragma: no cover
    _fromstring = ET.fromstring

log = logging.getLogger("pipeline")

FEED_URL = "https://www.youtube.com/feeds/videos.xml"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) deutsch-shorts/0.1"


class FeedError(Exception):
    """Feed could not be fetched or parsed."""


def normalize_ts(value: str | None) -> str | None:
    """'2026-09-01T10:00:00+00:00' -> '2026-09-01T10:00:00Z' (UTC, same shape as the Data API)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """Atom feed -> [{video_id, title, published_at}] newest first."""
    try:
        root = _fromstring(xml_text)
    except ET.ParseError as e:
        raise FeedError(f"feed XML unparsable: {e}") from e
    out: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", NS):
        vid = (entry.findtext("yt:videoId", default="", namespaces=NS) or "").strip()
        if not vid:
            continue
        out.append({
            "video_id": vid,
            "title": (entry.findtext("atom:title", default="", namespaces=NS) or "").strip(),
            "published_at": normalize_ts(entry.findtext("atom:published", default="", namespaces=NS)),
        })
    out.sort(key=lambda e: e["published_at"] or "", reverse=True)
    return out


def fetch_feed(params: dict[str, str], client: httpx.Client | None = None) -> list[dict[str, Any]]:
    own = client is None
    client = client or httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        try:
            r = client.get(FEED_URL, params=params)
        except httpx.HTTPError as e:
            raise FeedError(f"feed {params}: {e}") from e
    finally:
        if own:
            client.close()
    if r.status_code != 200:
        raise FeedError(f"feed {params}: HTTP {r.status_code}")
    return parse_feed(r.text)


def fetch_playlist_feed(playlist_id: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    return fetch_feed({"playlist_id": playlist_id}, client)


def fetch_channel_feed(channel_id: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    return fetch_feed({"channel_id": channel_id}, client)

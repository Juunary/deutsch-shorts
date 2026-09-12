"""YouTube Data API v3 client with a daily quota ledger.

Every call is charged to `quota_ledger` (one row per UTC day) *after* the request; the cap is
checked *before* it so a runaway loop can never burn more than `daily_cap` units. search.list
(100 units) is implemented for ad-hoc probes only and must never be used by the core pipeline.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Any, Callable

import httpx

from app.config import settings
from app.db import today

log = logging.getLogger("pipeline")

BASE_URL = "https://www.googleapis.com/youtube/v3/"
COSTS: dict[str, int] = {"channels": 1, "playlistItems": 1, "videos": 1, "search": 100}

_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_DUB_RE = re.compile(r"auto[- ]dubbed|automatically dubbed|automatisch synchronisiert", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


class QuotaExceeded(Exception):
    """Daily cap reached (local ledger) or reported by the API (403 quotaExceeded)."""


class PlaylistNotFound(Exception):
    """playlistItems.list answered 404 (e.g. the channel has no UUSH playlist)."""


class YouTubeAPIError(Exception):
    """Non-retryable API error (4xx) or retries exhausted."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _check_channel_id(channel_id: str) -> str:
    if not isinstance(channel_id, str) or len(channel_id) != 24 or not channel_id.startswith("UC"):
        raise ValueError(f"not a YouTube channel id: {channel_id!r}")
    return channel_id


def uush_id(channel_id: str) -> str:
    """Shorts-only playlist id: UC... -> UUSH..."""
    return "UUSH" + _check_channel_id(channel_id)[2:]


def uploads_id(channel_id: str) -> str:
    """All-uploads playlist id: UC... -> UU..."""
    return "UU" + _check_channel_id(channel_id)[2:]


def parse_iso8601_duration(value: str) -> int:
    """'PT1M2S' -> 62 seconds (supports D/H/M/S, fractional seconds, 'PT0S')."""
    m = _DURATION_RE.match((value or "").strip())
    if not m or not any(m.groups()):
        raise ValueError(f"bad ISO-8601 duration: {value!r}")
    d, h, mi, s = (m.group(k) for k in ("d", "h", "m", "s"))
    total = int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    return int(round(total))


def is_region_blocked(content_details: dict[str, Any] | None, region: str = "KR") -> bool:
    """True when `region` is in the blocked list, or an allowed list exists and lacks it."""
    rr = (content_details or {}).get("regionRestriction") or {}
    if region in (rr.get("blocked") or []):
        return True
    if rr.get("allowed") is not None:
        return region not in rr["allowed"]
    return False


def is_dub_description(text: str | None) -> bool:
    """Description mentions an automatic dub (YouTube multi-audio)."""
    return bool(text) and _DUB_RE.search(text) is not None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class YouTubeAPI:
    """Thin Data API v3 client. `conn` is used only for the quota ledger (may be None)."""

    def __init__(
        self,
        api_key: str | None = None,
        conn: sqlite3.Connection | None = None,
        daily_cap: int | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.youtube_api_key
        self.conn = conn
        self.daily_cap = int(daily_cap if daily_cap is not None else settings.quota_daily_cap)
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self.session_units = 0  # units charged through this instance
        self._client = httpx.Client(base_url=BASE_URL, transport=transport, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "YouTubeAPI":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- quota ledger ------------------------------------------------------
    def units_today(self) -> int:
        if self.conn is None:
            return self.session_units
        r = self.conn.execute("SELECT units FROM quota_ledger WHERE day=?", (today(),)).fetchone()
        return int(r[0]) if r else 0

    def _charge(self, cost: int, search: bool = False) -> None:
        self.session_units += cost
        if self.conn is None:
            return
        self.conn.execute(
            "INSERT INTO quota_ledger(day, units, search_calls) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET units = units + excluded.units, "
            "search_calls = search_calls + excluded.search_calls",
            (today(), cost, 1 if search else 0),
        )

    # ---- transport ---------------------------------------------------------
    def _get(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        cost = COSTS[resource]
        if not self.api_key:
            raise YouTubeAPIError("YOUTUBE_API_KEY is not configured")
        used = self.units_today()
        if used + cost > self.daily_cap:
            raise QuotaExceeded(f"quota cap: {used} used + {cost} > {self.daily_cap} units today")
        query = {k: v for k, v in params.items() if v is not None}
        query["key"] = self.api_key
        resp = self._request(resource, query)
        # Google charges every request the API processed, errors included.
        self._charge(cost, search=(resource == "search"))
        if resp.status_code >= 400:
            self._raise_for_status(resource, resp)
        return resp.json()

    def _request(self, resource: str, query: dict[str, Any]) -> httpx.Response:
        """GET with retries on network errors and 5xx (exponential backoff)."""
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._client.get(resource, params=query)
            except httpx.TransportError as e:
                last_error = e
                log.warning("%s: network error (%s), attempt %d", resource, e, attempt + 1)
            else:
                if resp.status_code < 500:
                    return resp
                last_error = YouTubeAPIError(f"{resource}: HTTP {resp.status_code}")
                log.warning("%s: HTTP %d, attempt %d", resource, resp.status_code, attempt + 1)
            if attempt < self.retries:
                self._sleep(self.backoff * (2 ** attempt))
        raise YouTubeAPIError(f"{resource}: giving up after {self.retries + 1} attempts: {last_error}") from last_error

    @staticmethod
    def _error_info(resp: httpx.Response) -> tuple[str, set[str]]:
        try:
            err = resp.json().get("error") or {}
        except ValueError:
            return resp.text[:200], set()
        reasons = {str(e.get("reason", "")) for e in (err.get("errors") or [])}
        return str(err.get("message", "")), reasons

    def _raise_for_status(self, resource: str, resp: httpx.Response) -> None:
        message, reasons = self._error_info(resp)
        if resp.status_code == 403 and reasons & _QUOTA_REASONS:
            raise QuotaExceeded(f"YouTube API: {message or 'quota exceeded'}")
        if resp.status_code == 404 and resource == "playlistItems":
            raise PlaylistNotFound(message or "playlist not found")
        raise YouTubeAPIError(f"{resource}: HTTP {resp.status_code} {sorted(reasons)} {message}".strip())

    # ---- endpoints ---------------------------------------------------------
    def resolve_handle(self, handle: str) -> dict[str, Any] | None:
        """channels.list(forHandle=...) -> {id, title, uploads_playlist_id} or None."""
        h = (handle or "").strip().lstrip("@")
        if not h:
            return None
        data = self._get("channels", {"part": "id,snippet,contentDetails", "forHandle": h})
        items = data.get("items") or []
        if not items:
            return None
        it = items[0]
        related = ((it.get("contentDetails") or {}).get("relatedPlaylists") or {})
        return {
            "id": it["id"],
            "title": (it.get("snippet") or {}).get("title"),
            "uploads_playlist_id": related.get("uploads"),
        }

    def list_playlist_items(
        self, playlist_id: str, page_token: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page (50) of playlist items -> ([{video_id, published_at}], next_page_token)."""
        data = self._get(
            "playlistItems",
            {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50, "pageToken": page_token},
        )
        items: list[dict[str, Any]] = []
        for it in data.get("items") or []:
            cd = it.get("contentDetails") or {}
            if cd.get("videoId"):
                items.append({"video_id": cd["videoId"], "published_at": cd.get("videoPublishedAt")})
        return items, data.get("nextPageToken")

    def videos_details(self, ids: list[str]) -> list[dict[str, Any]]:
        """videos.list(part=snippet,contentDetails,status) in batches of 50 -> raw items."""
        out: list[dict[str, Any]] = []
        unique = list(dict.fromkeys(i for i in ids if i))
        for i in range(0, len(unique), 50):
            batch = unique[i:i + 50]
            data = self._get(
                "videos",
                {"part": "snippet,contentDetails,status", "id": ",".join(batch), "maxResults": 50},
            )
            out.extend(data.get("items") or [])
        return out

    def search(
        self,
        q: str,
        channel_id: str | None = None,
        max_results: int = 25,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """search.list — 100 units per call. Probes only; never used by the core pipeline."""
        data = self._get(
            "search",
            {"part": "snippet", "q": q, "type": "video", "channelId": channel_id,
             "maxResults": max_results, "pageToken": page_token},
        )
        return data.get("items") or [], data.get("nextPageToken")

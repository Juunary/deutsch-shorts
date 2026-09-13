"""Machine translation for subtitles when no model output exists.

The default provider is Google Translate's public web endpoint - the one the translate.google.com page and
the Chrome extension call - so it needs no key. Requests are batched (one request per video and language,
up to GOOGLE_MAX_ITEMS segments / GOOGLE_MAX_CHARS characters each) because Google rate-limits bursts per IP;
a 429 ("unusual traffic" page) puts machine translation on a short cooldown shared by the pipeline and the
on-demand path in the app. YouTube's own caption translation offers English only and DeepL needs an API key,
so DeepL is opt-in (MT_PROVIDER=deepl). Translation rows keep the historical source tag "gtx" for Google.

  google - POST https://translate.googleapis.com/translate_a/t?client=gtx   (fallback: clients5.google.com)
  deepl  - DeepL API (free tier 500k chars/month; key ending in ':fx' -> api-free.deepl.com)
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

import httpx

from app.config import settings
from app.db import get_setting, set_setting, tx, utcnow

log = logging.getLogger("pipeline")

DEEPL_LANG = {"ko": "KO", "en": "EN-US", "de": "DE"}
GOOGLE_ENDPOINTS: list[tuple[str, str]] = [
    ("https://translate.googleapis.com/translate_a/t", "gtx"),
    ("https://clients5.google.com/translate_a/t", "dict-chrome-ex"),
]
GOOGLE_SOURCE = "gtx"
GOOGLE_MAX_ITEMS = 80
GOOGLE_MAX_CHARS = 4000
GOOGLE_SLEEP = 1.0                      # seconds between the chunks of one batch
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
COOLDOWN_KEY = "mt_cooldown_until"
COOLDOWN_MINUTES = 30
PROVIDER_ALIASES = {"google": "google", "gtx": "google", "auto": "google", "deepl": "deepl"}
SOURCE_TAG = {"google": GOOGLE_SOURCE, "deepl": "deepl"}


class TranslateError(Exception):
    """Provider unavailable (rate limit, auth, network)."""


class RateLimited(TranslateError):
    """HTTP 429 / abuse page: back off for a while."""


# ---- DeepL (opt-in) ----------------------------------------------------------
def _deepl_url(key: str) -> str:
    return "https://api-free.deepl.com/v2/translate" if key.endswith(":fx") else "https://api.deepl.com/v2/translate"


def translate_deepl(texts: list[str], tgt: str, src: str = "de", client: httpx.Client | None = None) -> list[str]:
    key = settings.deepl_api_key
    if not key:
        raise TranslateError("DEEPL_API_KEY not configured")
    own = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        r = client.post(_deepl_url(key), headers={"Authorization": f"DeepL-Auth-Key {key}"},
                        json={"text": texts, "source_lang": DEEPL_LANG.get(src, src.upper()),
                              "target_lang": DEEPL_LANG.get(tgt, tgt.upper())})
    except httpx.HTTPError as e:
        raise TranslateError(f"deepl: {e}") from e
    finally:
        if own:
            client.close()
    if r.status_code == 429:
        raise RateLimited("deepl: HTTP 429")
    if r.status_code != 200:
        raise TranslateError(f"deepl: HTTP {r.status_code} {r.text[:120]}")
    out = [t.get("text", "") for t in r.json().get("translations", [])]
    if len(out) != len(texts):
        raise TranslateError("deepl: response length mismatch")
    return out


# ---- Google (default) --------------------------------------------------------
def _chunks(texts: list[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for text in texts:
        if batch and (len(batch) >= GOOGLE_MAX_ITEMS or size + len(text) > GOOGLE_MAX_CHARS):
            yield batch
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        yield batch


def _parse_google(data: Any, n: int) -> list[str] | None:
    """translate_a/t answers ["t1", ...] for a fixed source language, [["t1", "de"], ...] for sl=auto,
    or a bare string when a single item was sent."""
    if isinstance(data, str):
        items: list[Any] = [data]
    elif isinstance(data, list):
        items = data
    else:
        return None
    out: list[str] = []
    for item in items:
        if isinstance(item, list):
            item = item[0] if item else ""
        if not isinstance(item, str):
            return None
        out.append(item)
    return out if len(out) == n else None


def _google_chunk(client: httpx.Client, chunk: list[str], tgt: str, src: str) -> list[str]:
    last: TranslateError | None = None
    for url, client_id in GOOGLE_ENDPOINTS:
        try:
            r = client.post(url, params={"client": client_id, "sl": src, "tl": tgt}, data={"q": chunk})
        except httpx.HTTPError as e:
            last = TranslateError(f"google: {e}")
            continue
        if r.status_code == 429:
            last = RateLimited(f"google: HTTP 429 ({url})")
            continue
        if r.status_code != 200:
            last = TranslateError(f"google: HTTP {r.status_code} ({url})")
            continue
        try:
            out = _parse_google(r.json(), len(chunk))
        except ValueError:
            out = None
        if out is None:
            last = TranslateError(f"google: unexpected response ({url})")
            continue
        return out
    raise last or TranslateError("google: no endpoint configured")


def translate_google(texts: list[str], tgt: str, src: str = "de", client: httpx.Client | None = None,
                     sleep: float = GOOGLE_SLEEP) -> list[str]:
    """Batch-translate `texts` (the segments of one video). One POST per chunk; falls back to the second host."""
    own = client is None
    client = client or httpx.Client(timeout=20.0, headers={"User-Agent": UA})
    out: list[str] = []
    try:
        for i, chunk in enumerate(_chunks(texts)):
            if i and sleep:
                time.sleep(sleep)
            out.extend(_google_chunk(client, chunk, tgt, src))
    finally:
        if own:
            client.close()
    return out


PROVIDERS: dict[str, Callable[..., list[str]]] = {"google": translate_google, "deepl": translate_deepl}


def pick_provider() -> str | None:
    """google (default; aliases gtx/auto) | deepl (explicit only) | None when MT_PROVIDER=none."""
    p = (settings.mt_provider or "google").lower()
    if p == "none":
        return None
    return PROVIDER_ALIASES.get(p)


def translate_batch(texts: list[str], tgt: str, provider: str | None = None, src: str = "de") -> tuple[str, list[str]]:
    """Translate `texts` -> (source tag for the translations table, translations). Raises TranslateError."""
    name = PROVIDER_ALIASES.get(provider.lower()) if provider else pick_provider()
    if not name:
        raise TranslateError("machine translation disabled (MT_PROVIDER=none)")
    if not texts:
        return SOURCE_TAG[name], []
    return SOURCE_TAG[name], PROVIDERS[name](texts, tgt, src)


# ---- cooldown shared by the pipeline and the app's on-demand path -------------
def cooldown_until(conn: sqlite3.Connection) -> str | None:
    until = get_setting(conn, COOLDOWN_KEY)
    return until if until and until > utcnow() else None


def set_cooldown(conn: sqlite3.Connection, minutes: int = COOLDOWN_MINUTES) -> str:
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with tx(conn):
        set_setting(conn, COOLDOWN_KEY, until)
    return until

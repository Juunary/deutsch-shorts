"""Machine-translation fallback for subtitles when no model output exists.

YouTube's own caption translation now offers English only for auto captions (and counts as extra
YouTube requests), so Korean falls back to:
  deepl  - DeepL API (free tier 500k chars/month; key ending in ':fx' -> api-free.deepl.com)
  gtx    - the unofficial Google Translate web endpoint (no key, best effort, rate-limited)
Provider order with MT_PROVIDER=auto: deepl if a key exists, else gtx.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

from app.config import settings

log = logging.getLogger("pipeline")

DEEPL_LANG = {"ko": "KO", "en": "EN-US", "de": "DE"}


class TranslateError(Exception):
    """Provider unavailable (rate limit, auth, network)."""


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
    if r.status_code != 200:
        raise TranslateError(f"deepl: HTTP {r.status_code} {r.text[:120]}")
    out = [t.get("text", "") for t in r.json().get("translations", [])]
    if len(out) != len(texts):
        raise TranslateError("deepl: response length mismatch")
    return out


def translate_gtx(texts: list[str], tgt: str, src: str = "de", client: httpx.Client | None = None,
                  sleep: float = 0.6) -> list[str]:
    own = client is None
    client = client or httpx.Client(timeout=15.0, headers={"User-Agent": "Mozilla/5.0"})
    out: list[str] = []
    try:
        for i, text in enumerate(texts):
            try:
                r = client.get("https://translate.googleapis.com/translate_a/single",
                               params={"client": "gtx", "sl": src, "tl": tgt, "dt": "t", "q": text})
            except httpx.HTTPError as e:
                raise TranslateError(f"gtx: {e}") from e
            if r.status_code != 200:
                raise TranslateError(f"gtx: HTTP {r.status_code}")
            data = r.json()
            out.append("".join(seg[0] for seg in (data[0] or []) if seg and seg[0]))
            if i < len(texts) - 1:
                time.sleep(sleep)
    finally:
        if own:
            client.close()
    return out


PROVIDERS: dict[str, Callable[..., list[str]]] = {"deepl": translate_deepl, "gtx": translate_gtx}


def pick_provider() -> str | None:
    p = (settings.mt_provider or "auto").lower()
    if p == "none":
        return None
    if p == "auto":
        return "deepl" if settings.deepl_api_key else "gtx"
    return p if p in PROVIDERS else None


def translate_batch(texts: list[str], tgt: str, provider: str | None = None, src: str = "de") -> tuple[str, list[str]]:
    """Translate `texts` -> (provider_name, translations). Raises TranslateError when nothing works."""
    name = provider or pick_provider()
    if not name:
        raise TranslateError("machine translation disabled (MT_PROVIDER=none)")
    if not texts:
        return name, []
    return name, PROVIDERS[name](texts, tgt, src)

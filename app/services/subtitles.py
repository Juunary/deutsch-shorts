"""Assemble the subtitle payload for one video: segments + best available translation + glosses.

Translation is always available: when a segment has no translation in the requested language yet (the
teacher/student model has not processed the video), the missing lines are machine-translated on demand with
the configured provider - Google Translate's free web endpoint by default, no API key - and cached in
`translations` (source "gtx"), so the app never shows a German-only card. Failures are logged and retried
after a pause; a Google rate limit sets a cooldown shared with the pipeline.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter

from app.models import GlossOut, SegmentOut, SubtitlesOut

log = logging.getLogger("app")

SOURCE_PRIORITY = ["user", "model", "deepl", "yt_mt", "gtx"]
FILL_RETRY_S = 600.0
_fill_backoff: dict[tuple[str, str], float] = {}     # (video_id, lang) -> monotonic time before which we don't retry


def pick_source(available: dict[str, str]) -> tuple[str | None, str | None]:
    for src in SOURCE_PRIORITY:
        if available.get(src):
            return src, available[src]
    if available:
        src = next(iter(available))
        return src, available[src]
    return None, None


def _translations(conn: sqlite3.Connection, video_id: str, lang: str) -> dict[int, dict[str, str]]:
    per_idx: dict[int, dict[str, str]] = {}
    for r in conn.execute("SELECT idx, source, text FROM translations WHERE video_id=? AND lang=?", (video_id, lang)):
        per_idx.setdefault(int(r["idx"]), {})[r["source"]] = r["text"]
    return per_idx


def fill_missing_translations(conn: sqlite3.Connection, video_id: str, lang: str) -> bool:
    """Machine-translate the segments lacking `lang` (best effort, cached in the DB). True when rows were added."""
    key = (video_id, lang)
    now = time.monotonic()
    if _fill_backoff.get(key, 0.0) > now:
        return False
    try:
        from pipeline.transcripts import translate_video
        from pipeline.translate import TranslateError
    except ImportError:
        return False
    try:
        done = translate_video(conn, video_id, langs=(lang,))
    except TranslateError as e:
        _fill_backoff[key] = now + FILL_RETRY_S
        log.warning("on-demand translation %s/%s failed: %s", video_id, lang, e)
        return False
    return bool(done)


def get_subtitles(conn: sqlite3.Connection, video_id: str, lang: str, fill: bool = True) -> SubtitlesOut | None:
    if conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone() is None:
        return None
    segs = conn.execute("SELECT idx, start_ms, end_ms, text_de, text_de_clean FROM segments WHERE video_id=? ORDER BY idx",
                        (video_id,)).fetchall()
    per_idx = _translations(conn, video_id, lang)
    if fill and segs and any(int(s["idx"]) not in per_idx for s in segs):
        if fill_missing_translations(conn, video_id, lang):
            per_idx = _translations(conn, video_id, lang)
    out_segs: list[SegmentOut] = []
    used: Counter[str] = Counter()
    for s in segs:
        src, text = pick_source(per_idx.get(int(s["idx"]), {}))
        if src:
            used[src] += 1
        out_segs.append(SegmentOut(idx=int(s["idx"]), start_ms=int(s["start_ms"]), end_ms=int(s["end_ms"]),
                                   de=s["text_de"], de_clean=s["text_de_clean"], tr=text))
    glosses = [GlossOut(surface=r["surface"], lemma=r["lemma"], pos=r["pos"], level=r["level"], ko=r["gloss_ko"],
                        en=r["gloss_en"], seg_idx=r["seg_idx"])
               for r in conn.execute("SELECT * FROM glosses WHERE video_id=? ORDER BY seg_idx, surface", (video_id,))]
    tr_source = used.most_common(1)[0][0] if used else None
    return SubtitlesOut(video_id=video_id, lang=lang, tr_source=tr_source, segments=out_segs, glosses=glosses)

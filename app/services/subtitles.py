"""Assemble the subtitle payload for one video: segments + best available translation + glosses."""
from __future__ import annotations

import sqlite3
from collections import Counter

from app.models import GlossOut, SegmentOut, SubtitlesOut

SOURCE_PRIORITY = ["user", "model", "deepl", "yt_mt", "gtx"]


def pick_source(available: dict[str, str]) -> tuple[str | None, str | None]:
    for src in SOURCE_PRIORITY:
        if available.get(src):
            return src, available[src]
    if available:
        src = next(iter(available))
        return src, available[src]
    return None, None


def get_subtitles(conn: sqlite3.Connection, video_id: str, lang: str) -> SubtitlesOut | None:
    if conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone() is None:
        return None
    segs = conn.execute("SELECT idx, start_ms, end_ms, text_de, text_de_clean FROM segments WHERE video_id=? ORDER BY idx",
                        (video_id,)).fetchall()
    per_idx: dict[int, dict[str, str]] = {}
    for r in conn.execute("SELECT idx, source, text FROM translations WHERE video_id=? AND lang=?", (video_id, lang)):
        per_idx.setdefault(int(r["idx"]), {})[r["source"]] = r["text"]
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

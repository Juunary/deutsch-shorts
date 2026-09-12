"""Cheap difficulty signals: A1/A2 vocabulary coverage, speech rate and a heuristic CEFR level.

Word levels come from optional Goethe word lists (data/wordlist_a1.txt, _a2, _b1; one word per
line) or, when absent, from the OpenSubtitles frequency rank in data/de_50k.txt.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from functools import lru_cache
from typing import Iterable

from app.config import ROOT
from app.db import tx

log = logging.getLogger("pipeline")

FREQ_FILE = ROOT / "data" / "de_50k.txt"
WORDLISTS = {"A1": ROOT / "data" / "wordlist_a1.txt", "A2": ROOT / "data" / "wordlist_a2.txt",
             "B1": ROOT / "data" / "wordlist_b1.txt"}
RANK_BANDS = [(1000, "A1"), (2500, "A2"), (5000, "B1"), (10000, "B2")]
LEVEL_PRIORITY = {"A1": 0, "A2": 1, "B1": 2, "B2": 3, "C1": 4}

_TOKEN_RE = re.compile(r"[a-zäöüß]+(?:['-][a-zäöüß]+)*")


@lru_cache(maxsize=1)
def frequency_ranks() -> dict[str, int]:
    ranks: dict[str, int] = {}
    if not FREQ_FILE.exists():
        log.warning("frequency list missing: %s", FREQ_FILE)
        return ranks
    with FREQ_FILE.open(encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            word = line.split(" ", 1)[0].strip().lower()
            if word and word not in ranks:
                ranks[word] = i
    return ranks


@lru_cache(maxsize=1)
def wordlists() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for level, path in WORDLISTS.items():
        if path.exists():
            out[level] = {w.strip().lower() for w in path.read_text(encoding="utf-8").splitlines() if w.strip()}
    return out


def tokenize_de(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def word_level(word: str) -> str:
    w = word.lower()
    lists = wordlists()
    for level in ("A1", "A2", "B1"):
        if level in lists and w in lists[level]:
            return level
    rank = frequency_ranks().get(w)
    if rank is None:
        return "C1"
    for limit, level in RANK_BANDS:
        if rank <= limit:
            return level
    return "C1"


def coverage(tokens: Iterable[str]) -> float:
    toks = list(tokens)
    if not toks:
        return 0.0
    easy = sum(1 for t in toks if word_level(t) in ("A1", "A2"))
    return easy / len(toks)


def words_per_second(segments: list[dict]) -> float:
    words = sum(len(tokenize_de(s.get("text_de") or "")) for s in segments)
    seconds = sum(max(0, int(s.get("end_ms", 0)) - int(s.get("start_ms", 0))) for s in segments) / 1000.0
    return words / seconds if seconds > 0 else 0.0


def estimate_cefr(cov: float, wps: float) -> str:
    if cov >= 0.85 and wps <= 2.4:
        return "A1"
    if cov >= 0.75:
        return "A2"
    if cov >= 0.62:
        return "B1"
    return "B2"


def run_heuristics(conn: sqlite3.Connection, video_id: str | None = None, force: bool = False) -> dict:
    """Compute coverage/wps and a heuristic CEFR for videos that have segments."""
    if video_id:
        vids = [video_id]
    else:
        sql = "SELECT DISTINCT v.id FROM videos v JOIN segments s ON s.video_id=v.id"
        if not force:
            sql += " WHERE v.a1a2_coverage IS NULL"
        vids = [r[0] for r in conn.execute(sql)]
    summary = {"computed": 0, "channel_fallback": 0}
    for vid in vids:
        segs = [dict(r) for r in conn.execute(
            "SELECT start_ms, end_ms, text_de FROM segments WHERE video_id=? ORDER BY idx", (vid,))]
        row = conn.execute(
            "SELECT v.cefr_source, c.level_hint FROM videos v JOIN channels c ON c.id=v.channel_id WHERE v.id=?",
            (vid,)).fetchone()
        if row is None:
            continue
        source, hint = row[0], row[1]
        tokens = [t for s in segs for t in tokenize_de(s["text_de"])]
        with tx(conn):
            if tokens:
                cov = coverage(tokens)
                wps = words_per_second(segs)
                conn.execute("UPDATE videos SET a1a2_coverage=?, wps=? WHERE id=?", (round(cov, 4), round(wps, 3), vid))
                if source in (None, "heuristic", "channel"):
                    conn.execute("UPDATE videos SET cefr=?, cefr_source='heuristic' WHERE id=?",
                                 (estimate_cefr(cov, wps), vid))
                summary["computed"] += 1
            elif source is None and hint:
                conn.execute("UPDATE videos SET cefr=?, cefr_source='channel' WHERE id=?", (hint, vid))
                summary["channel_fallback"] += 1
    return summary

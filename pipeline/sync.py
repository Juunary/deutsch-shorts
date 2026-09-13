"""Content sync between pipeline hosts.

The lab server (Korean university IP, not throttled by YouTube) collects transcripts and runs the teacher model;
the home PC serves the app. `export_content` dumps the CONTENT tables as JSON lines; `import_content` upserts them
into another database without touching user state (events, vocab, corrections, settings, affinities) and without
undoing the user's own edits (user translations, gloss removals, embed errors, user CEFR corrections).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.db import tx

log = logging.getLogger("pipeline")

CONTENT_TABLES = ["channels", "videos", "transcript_raw", "segments", "translations", "glosses", "enrichments"]
DEFAULT_EXPORT = ROOT / "data" / "content.jsonl"
STATUS_RANK = {"ok": 3, "fallback": 2, "failed": 1, "pending": 0, "none": 1, "disabled": 1, "skipped": 0, "mixed": 1}


def export_content(conn: sqlite3.Connection, path: Path = DEFAULT_EXPORT) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with path.open("w", encoding="utf-8") as f:
        for table in CONTENT_TABLES:
            n = 0
            for row in conn.execute(f"SELECT * FROM {table}"):
                f.write(json.dumps({"t": table, "r": dict(row)}, ensure_ascii=False) + "\n")
                n += 1
            counts[table] = n
    return counts


def _upsert_channel(conn: sqlite3.Connection, r: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO channels(id, handle, title, shorts_playlist_id, level_hint, topics_json, dubs, en_counterpart_id, "
        "enabled, last_ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "handle=COALESCE(excluded.handle, channels.handle), title=COALESCE(excluded.title, channels.title), "
        "shorts_playlist_id=COALESCE(excluded.shorts_playlist_id, channels.shorts_playlist_id), "
        "level_hint=COALESCE(excluded.level_hint, channels.level_hint), "
        "topics_json=CASE WHEN excluded.topics_json='[]' THEN channels.topics_json ELSE excluded.topics_json END, "
        "dubs=MAX(excluded.dubs, channels.dubs), en_counterpart_id=COALESCE(excluded.en_counterpart_id, channels.en_counterpart_id), "
        "last_ingested_at=MAX(COALESCE(excluded.last_ingested_at,''), COALESCE(channels.last_ingested_at,''))",
        (r["id"], r.get("handle"), r.get("title"), r.get("shorts_playlist_id"), r.get("level_hint"),
         r.get("topics_json") or "[]", int(r.get("dubs") or 0), r.get("en_counterpart_id"),
         int(r.get("enabled", 1)), r.get("last_ingested_at")),
    )


def _upsert_video(conn: sqlite3.Connection, r: dict[str, Any]) -> None:
    existing = conn.execute("SELECT transcript_status, enrich_status, cefr_source FROM videos WHERE id=?", (r["id"],)).fetchone()
    if existing is None:
        cols = ["id", "channel_id", "title", "description", "published_at", "duration_s", "default_audio_lang", "embeddable",
                "region_blocked", "has_dub", "pair_video_id", "transcript_status", "transcript_attempts", "next_transcript_try_at",
                "enrich_status", "cefr", "cefr_source", "a1a2_coverage", "wps", "topics_json", "summary_ko", "first_seen_at"]
        conn.execute(f"INSERT INTO videos({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                     tuple(r.get(c) if c not in ("topics_json",) else (r.get(c) or "[]") for c in cols))
        return
    t_status = r.get("transcript_status") if STATUS_RANK.get(r.get("transcript_status"), 0) >= STATUS_RANK.get(existing[0], 0) else existing[0]
    if r.get("transcript_status") == "mixed":   # heuristics verdict (mostly English speech) always wins over 'ok'
        t_status = "mixed"
    e_status = r.get("enrich_status") if STATUS_RANK.get(r.get("enrich_status"), 0) >= STATUS_RANK.get(existing[1], 0) else existing[1]
    take_cefr = existing[2] != "user" and (r.get("cefr_source") == "model" or existing[2] in (None, "heuristic", "channel"))
    conn.execute(
        "UPDATE videos SET title=COALESCE(?, title), description=COALESCE(?, description), published_at=COALESCE(?, published_at), "
        "duration_s=COALESCE(?, duration_s), default_audio_lang=COALESCE(?, default_audio_lang), "
        "embeddable=MIN(embeddable, ?), region_blocked=MAX(region_blocked, ?), has_dub=MAX(has_dub, ?), "
        "pair_video_id=COALESCE(?, pair_video_id), transcript_status=?, enrich_status=?, "
        "cefr=CASE WHEN ? THEN COALESCE(?, cefr) ELSE cefr END, cefr_source=CASE WHEN ? THEN COALESCE(?, cefr_source) ELSE cefr_source END, "
        "a1a2_coverage=COALESCE(?, a1a2_coverage), wps=COALESCE(?, wps), "
        "topics_json=CASE WHEN ?='[]' THEN topics_json ELSE ? END, summary_ko=COALESCE(?, summary_ko) WHERE id=?",
        (r.get("title"), r.get("description"), r.get("published_at"), r.get("duration_s"), r.get("default_audio_lang"),
         int(r.get("embeddable", 1)), int(r.get("region_blocked", 0)), int(r.get("has_dub", 0)), r.get("pair_video_id"),
         t_status, e_status, take_cefr, r.get("cefr"), take_cefr, r.get("cefr_source"), r.get("a1a2_coverage"), r.get("wps"),
         r.get("topics_json") or "[]", r.get("topics_json") or "[]", r.get("summary_ko"), r["id"]),
    )


def import_content(conn: sqlite3.Connection, path: Path = DEFAULT_EXPORT) -> dict[str, int]:
    """Upsert an export into this database. Segments and glosses are replaced per video, translations per
    (video, source); user rows are never touched."""
    counts: dict[str, int] = {t: 0 for t in CONTENT_TABLES}
    removed_glosses = {(r[0], str(r[1]).lower()) for r in conn.execute(
        "SELECT video_id, json_extract(after_json, '$.surface') FROM corrections WHERE kind='gloss' "
        "AND json_extract(after_json, '$.wrong') = 1")}
    cleared: dict[str, set[Any]] = {"segments": set(), "translations": set(), "glosses": set()}
    with tx(conn):
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                t, r = obj["t"], obj["r"]
                if t == "channels":
                    _upsert_channel(conn, r)
                elif t == "videos":
                    _upsert_video(conn, r)
                elif t == "transcript_raw":
                    conn.execute("INSERT OR REPLACE INTO transcript_raw(video_id, lang, is_generated, json, fetched_at) VALUES(?,?,?,?,?)",
                                 (r["video_id"], r["lang"], r.get("is_generated", 1), r["json"], r.get("fetched_at")))
                elif t == "segments":
                    if r["video_id"] not in cleared["segments"]:
                        conn.execute("DELETE FROM segments WHERE video_id=?", (r["video_id"],))
                        cleared["segments"].add(r["video_id"])
                    conn.execute("INSERT OR REPLACE INTO segments(video_id, idx, start_ms, end_ms, text_de, text_de_clean) VALUES(?,?,?,?,?,?)",
                                 (r["video_id"], r["idx"], r["start_ms"], r["end_ms"], r["text_de"], r.get("text_de_clean")))
                elif t == "translations":
                    if r.get("source") == "user":
                        continue
                    if (r["video_id"], r["source"]) not in cleared["translations"]:   # per source, so local on-demand
                        conn.execute("DELETE FROM translations WHERE video_id=? AND source=?", (r["video_id"], r["source"]))
                        cleared["translations"].add((r["video_id"], r["source"]))   # (gtx) rows survive an export without them
                    conn.execute("INSERT OR REPLACE INTO translations(video_id, idx, lang, source, text) VALUES(?,?,?,?,?)",
                                 (r["video_id"], r["idx"], r["lang"], r["source"], r["text"]))
                elif t == "glosses":
                    if r["video_id"] not in cleared["glosses"]:
                        conn.execute("DELETE FROM glosses WHERE video_id=?", (r["video_id"],))
                        cleared["glosses"].add(r["video_id"])
                    if (r["video_id"], str(r["surface_lc"]).lower()) in removed_glosses:
                        continue
                    conn.execute("INSERT OR REPLACE INTO glosses(video_id, surface_lc, surface, lemma, pos, level, gloss_ko, gloss_en, seg_idx) "
                                 "VALUES(?,?,?,?,?,?,?,?,?)",
                                 (r["video_id"], r["surface_lc"], r["surface"], r["lemma"], r["pos"], r.get("level"),
                                  r.get("gloss_ko"), r.get("gloss_en"), r.get("seg_idx")))
                elif t == "enrichments":
                    conn.execute("INSERT OR REPLACE INTO enrichments(video_id, backend, model, prompt_version, status, raw_json, "
                                 "input_tokens, output_tokens, cost_usd, latency_ms, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                 (r["video_id"], r["backend"], r.get("model"), r.get("prompt_version"), r["status"], r.get("raw_json"),
                                  r.get("input_tokens"), r.get("output_tokens"), r.get("cost_usd"), r.get("latency_ms"), r.get("created_at")))
                else:
                    continue
                counts[t] = counts.get(t, 0) + 1
    return counts


def pull(conn: sqlite3.Connection, remote: str = "Mustree", remote_path: str = "~/deutsch-shorts/data/content.jsonl",
         local_path: Path = DEFAULT_EXPORT) -> dict[str, int]:
    """scp the server's export here, then import it."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["scp", "-q", f"{remote}:{remote_path}", str(local_path)]
    log.info("pull: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return import_content(conn, local_path)

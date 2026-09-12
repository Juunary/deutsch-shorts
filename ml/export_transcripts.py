"""PC side: export transcripts (+ machine translations, corrections) from the app DB to ml/data/work/transcripts.jsonl.

    python ml/export_transcripts.py [--out ml/data/work/transcripts.jsonl] [--min-segments 2]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import WORK, ensure_dirs, write_jsonl  # noqa: E402  (sys.path bootstrap in common)


def export(out: Path, min_segments: int = 2) -> int:
    from app.db import open_db

    conn = open_db()
    videos = conn.execute(
        "SELECT v.id, v.channel_id, v.title, v.duration_s, v.cefr, v.cefr_source, v.topics_json, "
        "c.handle AS channel_handle, c.title AS channel_title, c.level_hint, c.topics_json AS channel_topics "
        "FROM videos v JOIN channels c ON c.id=v.channel_id WHERE v.transcript_status='ok' ORDER BY v.published_at DESC"
    ).fetchall()
    rows = []
    for v in videos:
        segs = conn.execute("SELECT idx, start_ms, end_ms, text_de FROM segments WHERE video_id=? ORDER BY idx", (v["id"],)).fetchall()
        if len(segs) < min_segments:
            continue
        tr = {}
        for t in conn.execute("SELECT idx, lang, source, text FROM translations WHERE video_id=?", (v["id"],)):
            tr.setdefault((t["lang"], t["source"]), {})[t["idx"]] = t["text"]
        corrections = [dict(c) for c in conn.execute("SELECT seg_idx, kind, before_json, after_json FROM corrections WHERE video_id=?", (v["id"],))]
        enr = conn.execute("SELECT backend, model, status, raw_json FROM enrichments WHERE video_id=? AND status='ok'", (v["id"],)).fetchone()
        enrichment = json.loads(enr["raw_json"]) if enr and enr["raw_json"] else None

        def aligned(lang: str, sources: tuple[str, ...]) -> list[str | None] | None:
            for src in sources:
                d = tr.get((lang, src))
                if d:
                    return [d.get(s["idx"]) for s in segs]
            return None

        rows.append({
            "video_id": v["id"], "channel_id": v["channel_id"], "channel_handle": v["channel_handle"],
            "channel_title": v["channel_title"], "level_hint": v["level_hint"], "cefr": v["cefr"], "cefr_source": v["cefr_source"],
            "channel_topics": json.loads(v["channel_topics"] or "[]"), "topics": json.loads(v["topics_json"] or "[]"),
            "title": v["title"], "duration_s": v["duration_s"],
            "segments": [{"i": s["idx"], "start_ms": s["start_ms"], "end_ms": s["end_ms"], "de": s["text_de"]} for s in segs],
            "mt_ko": aligned("ko", ("deepl", "yt_mt", "gtx")), "mt_en": aligned("en", ("deepl", "yt_mt", "gtx")),
            "user_ko": aligned("ko", ("user",)), "model_ko": aligned("ko", ("model",)), "model_en": aligned("en", ("model",)),
            "enrichment": enrichment, "enrich_backend": enr["backend"] if enr else None, "enrich_model": enr["model"] if enr else None,
            "gold": bool(corrections), "corrections": [
                {"seg_idx": c["seg_idx"], "kind": c["kind"], "before": json.loads(c["before_json"]) if c["before_json"] else None,
                 "after": json.loads(c["after_json"])} for c in corrections],
        })
    return write_jsonl(out, rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(WORK / "transcripts.jsonl"))
    ap.add_argument("--min-segments", type=int, default=2)
    a = ap.parse_args()
    ensure_dirs()
    n = export(Path(a.out), a.min_segments)
    print(f"wrote {n} videos -> {a.out}")


if __name__ == "__main__":
    main()

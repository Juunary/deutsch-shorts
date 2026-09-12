from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Query, Response

from app.auth import require_token, require_token_or_query
from app.db import tx, utcnow
from app.deps import get_db
from app.models import CorrectionIn

router = APIRouter(prefix="/api/corrections", tags=["corrections"])


@router.post("", dependencies=[Depends(require_token)])
def post_correction(c: CorrectionIn, db: sqlite3.Connection = Depends(get_db)) -> dict:
    with tx(db):
        cur = db.execute(
            "INSERT INTO corrections(video_id, seg_idx, kind, before_json, after_json, created_at) VALUES(?,?,?,?,?,?)",
            (c.video_id, c.seg_idx, c.kind, json.dumps(c.before, ensure_ascii=False) if c.before is not None else None,
             json.dumps(c.after, ensure_ascii=False), utcnow()),
        )
        cid = cur.lastrowid
        if c.kind == "translation" and c.seg_idx is not None:
            lang = str(c.after.get("lang") or "ko")
            text = str(c.after.get("text") or "").strip()
            if text:
                db.execute("INSERT OR REPLACE INTO translations(video_id, idx, lang, source, text) VALUES(?,?,?,'user',?)",
                           (c.video_id, c.seg_idx, lang, text))
        elif c.kind == "gloss":
            surface = str(c.after.get("surface") or "").strip().lower()
            if surface:
                if c.after.get("wrong"):
                    db.execute("DELETE FROM glosses WHERE video_id=? AND surface_lc=?", (c.video_id, surface))
                else:
                    sets = {k: c.after[k] for k in ("lemma", "pos", "level") if c.after.get(k)}
                    if c.after.get("ko"):
                        sets["gloss_ko"] = c.after["ko"]
                    if c.after.get("en"):
                        sets["gloss_en"] = c.after["en"]
                    if sets:
                        assign = ", ".join(f"{k}=?" for k in sets)
                        db.execute(f"UPDATE glosses SET {assign} WHERE video_id=? AND surface_lc=?",
                                   (*sets.values(), c.video_id, surface))
        elif c.kind == "level":
            cefr = str(c.after.get("cefr") or "")
            if cefr in ("A1", "A2", "B1", "B2", "C1"):
                db.execute("UPDATE videos SET cefr=?, cefr_source='user' WHERE id=?", (cefr, c.video_id))
    return {"ok": True, "id": cid}


@router.get("/export.jsonl", dependencies=[Depends(require_token_or_query)])
def export_corrections(unexported: int = Query(1), mark: int = Query(0), db: sqlite3.Connection = Depends(get_db)) -> Response:
    where = "WHERE c.exported_at IS NULL" if unexported else ""
    rows = db.execute(
        f"SELECT c.*, s.text_de FROM corrections c LEFT JOIN segments s ON s.video_id=c.video_id AND s.idx=c.seg_idx {where} "
        "ORDER BY c.id").fetchall()
    lines = []
    for r in rows:
        lines.append(json.dumps({
            "id": r["id"], "video_id": r["video_id"], "seg_idx": r["seg_idx"], "kind": r["kind"],
            "before": json.loads(r["before_json"]) if r["before_json"] else None,
            "after": json.loads(r["after_json"]), "created_at": r["created_at"], "text_de": r["text_de"],
        }, ensure_ascii=False))
    if mark and rows:
        with tx(db):
            db.execute(f"UPDATE corrections SET exported_at=? WHERE id IN ({','.join('?' * len(rows))})",
                       (utcnow(), *[r["id"] for r in rows]))
    body = ("\n".join(lines) + "\n") if lines else ""
    return Response(content=body, media_type="application/x-ndjson",
                    headers={"Content-Disposition": 'attachment; filename="corrections.jsonl"'})

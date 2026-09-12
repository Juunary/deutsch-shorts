from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.auth import require_token, require_token_or_query
from app.db import row, tx, utcnow
from app.deps import get_db
from app.models import VocabIn, VocabOut
from app.services.vocab_export import build_tsv

router = APIRouter(prefix="/api/vocab", tags=["vocab"])


def _out(r) -> VocabOut:
    d = dict(r)
    return VocabOut(**{k: d.get(k) for k in VocabOut.model_fields})


@router.get("", dependencies=[Depends(require_token)])
def list_vocab(db: sqlite3.Connection = Depends(get_db)) -> dict:
    rows = db.execute("SELECT * FROM vocab ORDER BY id DESC").fetchall()
    return {"items": [_out(r) for r in rows]}


@router.post("", dependencies=[Depends(require_token)], response_model=VocabOut)
def add_vocab(v: VocabIn, db: sqlite3.Connection = Depends(get_db)) -> VocabOut:
    lemma = v.lemma.strip()
    if not lemma:
        raise HTTPException(status_code=422, detail="lemma required")
    existing = db.execute("SELECT * FROM vocab WHERE lemma=? AND video_id IS ?", (lemma, v.video_id)).fetchone()
    if existing:
        return _out(existing)
    with tx(db):
        cur = db.execute(
            "INSERT INTO vocab(lemma, surface, pos, gloss_ko, gloss_en, video_id, seg_idx, sentence_de, sentence_ko, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (lemma, v.surface, v.pos, v.gloss_ko, v.gloss_en, v.video_id, v.seg_idx, v.sentence_de, v.sentence_ko, utcnow()),
        )
        if v.video_id:
            db.execute("INSERT INTO events(video_id, type, value, mode, created_at) VALUES(?,?,?,?,?)",
                       (v.video_id, "save_word", None, None, utcnow()))
    return _out(db.execute("SELECT * FROM vocab WHERE id=?", (cur.lastrowid,)).fetchone())


@router.delete("/{vocab_id}", dependencies=[Depends(require_token)])
def delete_vocab(vocab_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    with tx(db):
        db.execute("DELETE FROM vocab WHERE id=?", (vocab_id,))
    return {"ok": True}


@router.get("/export.tsv", dependencies=[Depends(require_token_or_query)])
def export_tsv(unexported: int = Query(1), mark: int = Query(0), db: sqlite3.Connection = Depends(get_db)) -> Response:
    where = "WHERE v.exported_at IS NULL" if unexported else ""
    rows = db.execute(
        f"SELECT v.*, s.start_ms, c.handle FROM vocab v "
        "LEFT JOIN segments s ON s.video_id=v.video_id AND s.idx=v.seg_idx "
        "LEFT JOIN videos vi ON vi.id=v.video_id LEFT JOIN channels c ON c.id=vi.channel_id "
        f"{where} ORDER BY v.id").fetchall()
    body = build_tsv([dict(r) for r in rows])
    if mark and rows:
        with tx(db):
            db.execute(f"UPDATE vocab SET exported_at=? WHERE id IN ({','.join('?' * len(rows))})",
                       (utcnow(), *[r["id"] for r in rows]))
    return Response(content=body, media_type="text/tab-separated-values; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="vocab.tsv"'})

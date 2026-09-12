from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.auth import require_token
from app.deps import get_db
from app.services.takeout import apply_weights, parse_watch_history, topic_weights

router = APIRouter(prefix="/api/import", tags=["import"], dependencies=[Depends(require_token)])
MAX_BYTES = 20 * 1024 * 1024


@router.post("/takeout")
def import_takeout(file: UploadFile = File(...), db: sqlite3.Connection = Depends(get_db)) -> dict:
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 20 MB)")
    entries = parse_watch_history(data)
    if not entries:
        raise HTTPException(status_code=422, detail="no YouTube watch-history entries found")
    weights = topic_weights(entries)
    apply_weights(db, weights)
    return {"topic_weights": weights, "titles_used": len(entries)}

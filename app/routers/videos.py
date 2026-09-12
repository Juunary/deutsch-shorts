from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_token
from app.config import settings
from app.deps import get_db
from app.models import SubtitlesOut
from app.services.subtitles import get_subtitles

log = logging.getLogger("app")
router = APIRouter(prefix="/api/videos", tags=["videos"], dependencies=[Depends(require_token)])


@router.get("/{video_id}/subtitles", response_model=SubtitlesOut)
def subtitles(video_id: str, lang: str = Query("ko", pattern="^(ko|en)$"), db: sqlite3.Connection = Depends(get_db)) -> SubtitlesOut:
    out = get_subtitles(db, video_id, lang)
    if out is None:
        raise HTTPException(status_code=404, detail="video not found")
    return out


@router.post("/{video_id}/enrich")
def enrich(video_id: str, db: sqlite3.Connection = Depends(get_db)) -> dict[str, str]:
    if settings.llm_backend == "none":
        return {"status": "disabled"}
    try:
        from pipeline.enrich import enrich_one
        from pipeline.llm_backends import get_backend
    except ImportError:
        return {"status": "disabled"}
    backend = get_backend()
    if not backend.is_ready():
        return {"status": "disabled"}
    return {"status": enrich_one(db, video_id, backend=backend)}

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from app.auth import require_token
from app.deps import get_db
from app.models import FeedOut
from app.services.scoring import build_feed

router = APIRouter(prefix="/api", tags=["feed"], dependencies=[Depends(require_token)])


@router.get("/feed", response_model=FeedOut)
def feed(n: int = Query(10, ge=1, le=50), exclude: str = Query(""), db: sqlite3.Connection = Depends(get_db)) -> FeedOut:
    ids = {x.strip() for x in exclude.split(",") if x.strip()}
    return FeedOut(items=build_feed(db, n=n, exclude=ids))

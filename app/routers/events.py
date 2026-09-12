from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.auth import require_token
from app.deps import get_db
from app.models import EventIn
from app.services.feedback import apply_events

router = APIRouter(prefix="/api", tags=["events"], dependencies=[Depends(require_token)])


@router.post("/events")
def post_events(events: list[EventIn], db: sqlite3.Connection = Depends(get_db)) -> dict:
    return {"ok": True, "applied": apply_events(db, events)}

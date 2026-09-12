from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_token
from app.db import tx
from app.deps import get_db
from app.models import ChannelOut
from app.services.common import json_list

router = APIRouter(prefix="/api/channels", tags=["channels"], dependencies=[Depends(require_token)])

_SQL = (
    "SELECT c.*, (SELECT count(*) FROM videos v WHERE v.channel_id=c.id) AS video_count FROM channels c "
    "WHERE c.enabled=1 OR c.id NOT IN (SELECT en_counterpart_id FROM channels WHERE en_counterpart_id IS NOT NULL) "
    "ORDER BY c.level_hint, c.handle"
)


class ChannelPatch(BaseModel):
    enabled: bool


def _out(r) -> ChannelOut:
    return ChannelOut(id=r["id"], handle=r["handle"], title=r["title"], level_hint=r["level_hint"],
                      topics=json_list(r["topics_json"]), enabled=bool(r["enabled"]), video_count=int(r["video_count"]))


@router.get("")
def list_channels(db: sqlite3.Connection = Depends(get_db)) -> dict:
    return {"items": [_out(r) for r in db.execute(_SQL)]}


@router.put("/{channel_id}", response_model=ChannelOut)
def toggle_channel(channel_id: str, patch: ChannelPatch, db: sqlite3.Connection = Depends(get_db)) -> ChannelOut:
    with tx(db):
        cur = db.execute("UPDATE channels SET enabled=? WHERE id=?", (1 if patch.enabled else 0, channel_id))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="channel not found")
    r = db.execute("SELECT c.*, (SELECT count(*) FROM videos v WHERE v.channel_id=c.id) AS video_count FROM channels c WHERE c.id=?",
                   (channel_id,)).fetchone()
    return _out(r)

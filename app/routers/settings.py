from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from app.auth import require_token
from app.db import all_settings, set_setting, tx, utcnow
from app.deps import get_db
from app.models import SettingsPatch, TopicOut
from app.taxonomy import load_topics, topic_ids

router = APIRouter(prefix="/api", tags=["settings"], dependencies=[Depends(require_token)])
KNOWN_KEYS = {"subtitle_lang", "mode", "stretch", "playback_rate", "reveal_de_on_tap", "prefer_dub", "llm_enabled", "onboarded"}


def _settings_out(db: sqlite3.Connection) -> dict[str, Any]:
    out = all_settings(db)
    out["topics"] = [r[0] for r in db.execute("SELECT topic FROM topic_affinity WHERE base > 0 ORDER BY base DESC, topic")]
    return out


@router.get("/settings")
def get_settings(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    return _settings_out(db)


@router.put("/settings")
def put_settings(patch: SettingsPatch, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    data = patch.model_dump(exclude_unset=True)
    with tx(db):
        for k, v in data.items():
            if k in KNOWN_KEYS and v is not None:
                set_setting(db, k, v)
        if data.get("topics") is not None:
            chosen = {t for t in data["topics"] if t in topic_ids()}
            for t in topic_ids():
                db.execute("INSERT INTO topic_affinity(topic, base, learned, updated_at) VALUES(?, ?, 0, ?) "
                           "ON CONFLICT(topic) DO UPDATE SET base=excluded.base, updated_at=excluded.updated_at",
                           (t, 1.0 if t in chosen else 0.0, utcnow()))
    return _settings_out(db)


@router.get("/topics")
def topics() -> dict[str, list[TopicOut]]:
    return {"items": [TopicOut(id=t["id"], label_ko=t.get("label_ko", t["id"])) for t in load_topics()]}

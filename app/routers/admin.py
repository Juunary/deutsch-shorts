from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_token
from app.config import APP_VERSION, ROOT, settings
from app.db import rows
from app.deps import get_db
from app.models import HealthOut

log = logging.getLogger("app")
router = APIRouter(prefix="/api", tags=["admin"], dependencies=[Depends(require_token)])
STAGES = {"seed", "ingest", "backfill", "transcripts", "translate", "heuristics", "enrich", "pair", "all"}
_llm_cache: dict[str, Any] = {"at": 0.0, "ready": False}


def llm_ready() -> bool:
    backend = settings.llm_backend
    if backend == "claude":
        import os
        return bool(settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))
    if backend != "local":
        return False
    if time.time() - _llm_cache["at"] < 60:
        return bool(_llm_cache["ready"])
    ready = False
    try:
        base = settings.local_llm_url.rstrip("/")
        for path in ("/api/tags", "/v1/models"):
            r = httpx.get(base + path, timeout=1.5)
            if r.status_code == 200:
                ready = True
                break
    except httpx.HTTPError:
        ready = False
    _llm_cache.update(at=time.time(), ready=ready)
    return ready


@router.get("/health", response_model=HealthOut)
def health(db: sqlite3.Connection = Depends(get_db)) -> HealthOut:
    counts = {
        "channels": db.execute("SELECT count(*) FROM channels WHERE enabled=1").fetchone()[0],
        "videos": db.execute("SELECT count(*) FROM videos").fetchone()[0],
        "transcripts": db.execute("SELECT count(*) FROM videos WHERE transcript_status='ok'").fetchone()[0],
        "enriched": db.execute("SELECT count(*) FROM videos WHERE enrich_status='ok'").fetchone()[0],
        "vocab": db.execute("SELECT count(*) FROM vocab").fetchone()[0],
    }
    return HealthOut(ok=True, version=APP_VERSION, llm_backend=settings.llm_backend, llm_ready=llm_ready(), counts=counts)


def _running(db: sqlite3.Connection) -> bool:
    r = db.execute("SELECT started_at FROM pipeline_runs WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return False
    from app.services.common import parse_ts
    started = parse_ts(r[0])
    return bool(started) and (time.time() - started.timestamp()) < 600


@router.post("/admin/pipeline/run")
def run_pipeline(stage: str = Query("all"), db: sqlite3.Connection = Depends(get_db)) -> dict:
    if stage not in STAGES:
        raise HTTPException(status_code=400, detail=f"unknown stage {stage}")
    if _running(db):
        raise HTTPException(status_code=409, detail="pipeline already running")
    (ROOT / "logs").mkdir(exist_ok=True)
    logf = open(ROOT / "logs" / "pipeline-manual.log", "ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen([sys.executable, "-m", "pipeline", stage], cwd=str(ROOT), stdout=logf,
                            stderr=subprocess.STDOUT, creationflags=flags)
    log.info("pipeline %s started pid %s", stage, proc.pid)
    return {"ok": True, "pid": proc.pid}


@router.get("/admin/pipeline/status")
def pipeline_status(db: sqlite3.Connection = Depends(get_db)) -> dict:
    runs = rows(db.execute("SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 20"))
    return {"runs": runs, "running": _running(db)}

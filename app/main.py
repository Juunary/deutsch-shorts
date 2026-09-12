"""FastAPI application factory. Run with:  uvicorn app.main:app --port 8000"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import APP_VERSION, ROOT, settings
from .db import open_db
from .deps import mark_migrated
from .routers import admin, channels, corrections, events, feed, import_takeout, settings as settings_router, videos, vocab

log = logging.getLogger("app")
STATIC_DIR = ROOT / "static"
SPIKES_DIR = ROOT / "spikes"
PLACEHOLDER = "<!doctype html><meta charset=utf-8><title>deutsch-shorts</title><p>frontend not built yet (static/index.html missing)</p>"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = open_db()
    mark_migrated(settings.db_file)
    conn.close()
    log.info("deutsch-shorts %s ready; db=%s llm_backend=%s", APP_VERSION, settings.db_file, settings.llm_backend)
    yield


def _file(path: Path, media_type: str, extra: dict[str, str] | None = None):
    if not path.exists():
        return None
    headers = {"Cache-Control": "no-store"}
    headers.update(extra or {})
    return FileResponse(path, media_type=media_type, headers=headers)


def create_app() -> FastAPI:
    app = FastAPI(title="deutsch-shorts", version=APP_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def no_store_api(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    for r in (admin, feed, videos, events, corrections, vocab, settings_router, channels, import_takeout):
        app.include_router(r.router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    if SPIKES_DIR.exists():
        app.mount("/spikes", StaticFiles(directory=str(SPIKES_DIR), html=True), name="spikes")

    @app.get("/", include_in_schema=False)
    def index():
        return _file(STATIC_DIR / "index.html", "text/html; charset=utf-8") or HTMLResponse(PLACEHOLDER, headers={"Cache-Control": "no-store"})

    @app.get("/sw.js", include_in_schema=False)
    def sw():
        return _file(STATIC_DIR / "sw.js", "application/javascript", {"Service-Worker-Allowed": "/"}) or JSONResponse({"detail": "missing"}, 404)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return _file(STATIC_DIR / "manifest.webmanifest", "application/manifest+json") or JSONResponse({"detail": "missing"}, 404)

    return app


app = create_app()

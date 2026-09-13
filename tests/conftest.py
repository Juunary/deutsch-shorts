"""Shared pytest fixtures.

`db`        - fresh migrated SQLite database in a temp dir.
`seeded_db` - `db` plus one channel and two videos with segments + machine translations.
`client`    - FastAPI TestClient bound to a fresh database, with the auth header pre-set.
`seed_data` - importable helper to seed any connection the same way.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app import config

CHANNEL_ID = "UCtest000000000000000000"
VIDEO_IDS = ("vid00000001", "vid00000002")


def seed_data(conn: sqlite3.Connection) -> None:
    from app.db import tx

    with tx(conn):
        conn.execute(
            "INSERT INTO channels(id, handle, title, shorts_playlist_id, level_hint, topics_json) VALUES(?,?,?,?,?,?)",
            (CHANNEL_ID, "EasyGerman", "Easy German", "UUSHtest000000000000000000", "A2",
             json.dumps(["daily_life", "culture"])),
        )
        for vid, title in zip(VIDEO_IDS, ("Guten Morgen", "Was isst du?")):
            conn.execute(
                "INSERT INTO videos(id, channel_id, title, description, published_at, duration_s, "
                "transcript_status, cefr, cefr_source, topics_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (vid, CHANNEL_ID, title, "", "2026-09-01T00:00:00Z", 45, "ok", "A2", "heuristic",
                 json.dumps(["daily_life"])),
            )
            segs = [(0, 0, 2000, "guten morgen"), (1, 2000, 4500, "wie geht es dir"), (2, 4500, 7000, "ich trinke kaffee")]
            for idx, s, e, text in segs:
                conn.execute("INSERT INTO segments(video_id, idx, start_ms, end_ms, text_de) VALUES(?,?,?,?,?)",
                             (vid, idx, s, e, text))
                for lang in ("ko", "en"):
                    conn.execute("INSERT INTO translations(video_id, idx, lang, source, text) VALUES(?,?,?,?,?)",
                                 (vid, idx, lang, "deepl", f"[{lang} {idx}]"))


@pytest.fixture
def db(tmp_path):
    from app.db import open_db

    conn = open_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def seeded_db(db):
    seed_data(db)
    return db


@pytest.fixture
def client(tmp_path):
    """TestClient with a fresh DB (seed it via `seed_data(open_db(config.settings.db_file))`)."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    old_db, old_token = config.settings.db_path, config.settings.app_token
    config.settings.db_path = str(tmp_path / "api.db")
    config.settings.app_token = "test-token"
    app = create_app()
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer test-token"})
        yield c
    config.settings.db_path, config.settings.app_token = old_db, old_token


@pytest.fixture(autouse=True)
def _no_real_machine_translation(monkeypatch):
    """Tests never call Google/DeepL: the provider table raises unless a test overrides it."""
    from pipeline import translate as TR

    def blocked(*args, **kwargs):
        raise TR.TranslateError("network disabled in tests")
    monkeypatch.setitem(TR.PROVIDERS, "google", blocked)
    monkeypatch.setitem(TR.PROVIDERS, "deepl", blocked)

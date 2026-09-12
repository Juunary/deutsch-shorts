"""SQLite access: connection factory, migrations, small helpers.

Design: one connection per request/worker (SQLite connections are cheap), WAL mode so the
pipeline process can write while the API reads, autocommit mode with explicit transactions
via `tx()`.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import ROOT, settings

SCHEMA_DIR = ROOT / "app" / "schema"


def utcnow() -> str:
    """ISO-8601 UTC timestamp with milliseconds, e.g. 2026-09-12T08:30:00.000Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else settings.db_file
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if str(p) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply app/schema/NNN_*.sql files newer than PRAGMA user_version. Returns final version."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for f in sorted(SCHEMA_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        n = int(f.name[:3])
        if n <= current:
            continue
        conn.executescript(f.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version={n}")
        current = n
    return current


def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (connection is in autocommit mode otherwise)."""
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def rows(cur_or_rows) -> list[dict[str, Any]]:
    return [dict(r) for r in cur_or_rows]


def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


# ---- settings table helpers -------------------------------------------------

DEFAULT_SETTINGS: dict[str, Any] = {
    "subtitle_lang": "ko",       # ko | en
    "mode": "listening",         # listening | reading
    "stretch": False,            # allow A2-B1 content
    "playback_rate": 1.0,        # 0.75 | 1.0
    "reveal_de_on_tap": True,
    "prefer_dub": False,
    "llm_enabled": True,
    "transcript_cooldown_until": None,
    "onboarded": False,
}


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    r = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    if r is None:
        return DEFAULT_SETTINGS.get(key, default)
    return json.loads(r[0])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key, value_json) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for r in conn.execute("SELECT key, value_json FROM settings"):
        out[r[0]] = json.loads(r[1])
    return out

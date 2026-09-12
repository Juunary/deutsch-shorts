"""FastAPI dependencies."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterator

from .config import settings
from .db import connect, migrate

_migrated: set[str] = set()
_lock = threading.Lock()


def mark_migrated(path: str | Path) -> None:
    """Record that `path` has been migrated (called by the app lifespan after open_db())."""
    with _lock:
        _migrated.add(str(path))


def get_db() -> Iterator[sqlite3.Connection]:
    """Yield a fresh autocommit connection for the request and close it afterwards.

    The DB is migrated at startup; `migrate()` runs lazily once per process per path as a safety net.
    """
    path = settings.db_file  # read at request time, never at import time
    conn = connect(path)
    try:
        key = str(path)
        if key not in _migrated:
            with _lock:
                if key not in _migrated:
                    migrate(conn)
                    _migrated.add(key)
        yield conn
    finally:
        conn.close()

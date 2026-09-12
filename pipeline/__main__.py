"""Pipeline CLI.

    python -m pipeline <stage> [--limit N] [--video ID] [--full] [--retry-failed] [--force] [--no-translate]

Stages: seed | ingest | backfill | transcripts | translate | heuristics | enrich | pair | decay | all | stats
        export (content tables -> data/content.jsonl) | import --file X | pull (scp the server export + import)
`all` = seed -> ingest (RSS when no API key) -> transcripts -> heuristics -> enrich -> pair -> decay.
Every stage writes a pipeline_runs row. Logs go to stdout and logs/pipeline-YYYYMMDD.log.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.config import ROOT, settings
from app.db import open_db, tx, utcnow

log = logging.getLogger("pipeline")
STAGES = ["seed", "ingest", "backfill", "transcripts", "translate", "heuristics", "enrich", "pair", "decay", "all", "stats",
          "export", "import", "pull"]


def setup_logging() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(logs / f"pipeline-{datetime.now():%Y%m%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _units_today(conn: sqlite3.Connection) -> int:
    from app.db import today
    r = conn.execute("SELECT units FROM quota_ledger WHERE day=?", (today(),)).fetchone()
    return int(r[0]) if r else 0


def run_stage(conn: sqlite3.Connection, name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = utcnow()
    units_before = _units_today(conn)
    log.info("=== %s start", name)
    summary: dict[str, Any] = {}
    ok = True
    try:
        summary = fn() or {}
    except Exception as e:  # keep the other stages running
        ok = False
        summary = {"error": f"{type(e).__name__}: {e}"}
        log.exception("stage %s crashed", name)
    with tx(conn):
        conn.execute(
            "INSERT INTO pipeline_runs(stage, started_at, finished_at, quota_units, ok, failed, llm_calls, "
            "llm_cost_usd, notes) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, started, utcnow(), _units_today(conn) - units_before,
             int(summary.get("ok", 1 if ok else 0) or 0), int(summary.get("failed", 0) or 0),
             int(summary.get("llm_calls", 0) or 0), float(summary.get("cost_usd", 0.0) or 0.0),
             json.dumps(summary, ensure_ascii=False)[:2000]),
        )
    log.info("=== %s done: %s", name, json.dumps(summary, ensure_ascii=False))
    return summary


def stage_seed(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .ingest import seed
    from .youtube_api import YouTubeAPI
    with YouTubeAPI(conn=conn) as api:
        return seed(conn, api if api.api_key else None)


def stage_ingest(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .ingest import ingest, ingest_rss
    from .youtube_api import YouTubeAPI
    if not settings.youtube_api_key:
        log.info("no YOUTUBE_API_KEY: using RSS discovery (15 newest shorts per channel)")
        return ingest_rss(conn, limit_channels=args.limit)
    with YouTubeAPI(conn=conn) as api:
        return ingest(conn, api, full=args.full, limit_channels=args.limit)


def stage_backfill(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .ingest import backfill_details
    from .youtube_api import YouTubeAPI
    if not settings.youtube_api_key:
        return {"error": "YOUTUBE_API_KEY required"}
    with YouTubeAPI(conn=conn) as api:
        return backfill_details(conn, api, limit=args.limit or 500)


def stage_transcripts(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .transcripts import run_transcripts
    return run_transcripts(conn, limit=args.limit or 30, video_id=args.video, translate=not args.no_translate)


def stage_translate(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .transcripts import run_translate
    return run_translate(conn, limit=args.limit or 200)


def stage_heuristics(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .heuristics import run_heuristics
    return run_heuristics(conn, video_id=args.video, force=args.force)


def stage_enrich(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .enrich import run_enrich
    return run_enrich(conn, limit=args.limit, video_id=args.video, retry_failed=args.retry_failed, force=args.force,
                      workers=args.workers)


def stage_pair(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .pair import match_pairs
    return match_pairs(conn)


def stage_decay(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    try:
        from app.services.feedback import decay
    except ImportError:
        return {"skipped": "app.services.feedback not available"}
    return decay(conn)


def stage_export(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .sync import DEFAULT_EXPORT, export_content
    out = Path(args.out) if args.out else DEFAULT_EXPORT
    counts = export_content(conn, out)
    log.info("exported to %s", out)
    return counts


def stage_import(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .sync import DEFAULT_EXPORT, import_content
    return import_content(conn, Path(args.file) if args.file else DEFAULT_EXPORT)


def stage_pull(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    from .sync import pull
    return pull(conn, remote=args.remote, remote_path=args.remote_path)


def stage_stats(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    def counts(sql: str) -> dict[str, int]:
        return {str(r[0]): int(r[1]) for r in conn.execute(sql)}
    stats = {
        "channels_enabled": conn.execute("SELECT count(*) FROM channels WHERE enabled=1").fetchone()[0],
        "channels_total": conn.execute("SELECT count(*) FROM channels").fetchone()[0],
        "videos": conn.execute("SELECT count(*) FROM videos").fetchone()[0],
        "by_transcript_status": counts("SELECT transcript_status, count(*) FROM videos GROUP BY 1"),
        "by_enrich_status": counts("SELECT enrich_status, count(*) FROM videos WHERE transcript_status='ok' GROUP BY 1"),
        "by_cefr": counts("SELECT COALESCE(cefr,'?'), count(*) FROM videos WHERE transcript_status='ok' GROUP BY 1"),
        "translations_by_source": counts("SELECT source, count(*) FROM translations GROUP BY 1"),
        "quota_units_today": _units_today(conn),
        "vocab": conn.execute("SELECT count(*) FROM vocab").fetchone()[0],
        "corrections": conn.execute("SELECT count(*) FROM corrections").fetchone()[0],
        "llm_cost_total_usd": round(conn.execute("SELECT COALESCE(sum(cost_usd),0) FROM enrichments").fetchone()[0], 4),
    }
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


STAGE_FUNCS = {
    "seed": stage_seed, "ingest": stage_ingest, "backfill": stage_backfill, "transcripts": stage_transcripts,
    "translate": stage_translate, "heuristics": stage_heuristics, "enrich": stage_enrich, "pair": stage_pair,
    "decay": stage_decay, "stats": stage_stats, "export": stage_export, "import": stage_import, "pull": stage_pull,
}
ALL_ORDER = ["seed", "ingest", "transcripts", "heuristics", "enrich", "pair", "decay"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--video", default=None, help="restrict transcripts/heuristics/enrich to one video id")
    p.add_argument("--full", action="store_true", help="ingest: page through the backfill window even for known channels")
    p.add_argument("--retry-failed", action="store_true", help="enrich: include fallback/failed videos")
    p.add_argument("--force", action="store_true", help="recompute even if already done")
    p.add_argument("--no-translate", action="store_true", help="transcripts: skip machine translation")
    p.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    p.add_argument("--workers", type=int, default=None, help="enrich: parallel requests (default ENRICH_WORKERS)")
    p.add_argument("--out", default=None, help="export: output path (default data/content.jsonl)")
    p.add_argument("--file", default=None, help="import: input path (default data/content.jsonl)")
    p.add_argument("--remote", default="Mustree", help="pull: ssh host alias")
    p.add_argument("--remote-path", default="~/deutsch-shorts/data/content.jsonl", help="pull: remote export path")
    args = p.parse_args(argv)
    setup_logging()
    if args.dry_run:
        print("would run:", ALL_ORDER if args.stage == "all" else [args.stage], vars(args))
        return 0
    conn = open_db()
    try:
        if args.stage == "stats":
            stage_stats(conn, args)
            return 0
        stages = ALL_ORDER if args.stage == "all" else [args.stage]
        for name in stages:
            run_stage(conn, name, lambda name=name: STAGE_FUNCS[name](conn, args))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

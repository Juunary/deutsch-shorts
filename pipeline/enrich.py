"""Enrichment stage: run the LLM task on transcripts and store translations, glosses, CEFR, topics."""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from app.config import settings
from app.db import tx, utcnow
from app.models import PROMPT_VERSION, Enrichment, EnrichmentValidationError, validate_enrichment
from app.taxonomy import topic_ids

from .llm_backends import BackendError, Usage, build_payload, get_backend, load_system_prompt
from .transcripts import LEVEL_ORDER_SQL

log = logging.getLogger("pipeline")


def _record(conn: sqlite3.Connection, video_id: str, backend_name: str, status: str,
            usage: Usage | None, raw: str | None) -> None:
    u = usage or Usage()
    conn.execute(
        "INSERT INTO enrichments(video_id, backend, model, prompt_version, status, raw_json, input_tokens, "
        "output_tokens, cost_usd, latency_ms, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(video_id) DO UPDATE SET backend=excluded.backend, model=excluded.model, "
        "prompt_version=excluded.prompt_version, status=excluded.status, raw_json=excluded.raw_json, "
        "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, cost_usd=excluded.cost_usd, "
        "latency_ms=excluded.latency_ms, created_at=excluded.created_at",
        (video_id, backend_name, u.model, PROMPT_VERSION, status, raw, u.input_tokens, u.output_tokens,
         u.cost_usd, u.latency_ms, utcnow()),
    )
    conn.execute("UPDATE videos SET enrich_status=? WHERE id=?", (status, video_id))


def _seg_index(segments_de: list[str], surface: str) -> int | None:
    s = surface.lower()
    for i, t in enumerate(segments_de):
        if s in t.lower():
            return i
    return None


def store_enrichment(conn: sqlite3.Connection, video_id: str, enr: Enrichment, segments_de: list[str],
                     backend_name: str, usage: Usage) -> None:
    with tx(conn):
        for seg in enr.segments:
            conn.execute("UPDATE segments SET text_de_clean=? WHERE video_id=? AND idx=?", (seg.de_clean, video_id, seg.i))
        conn.execute("DELETE FROM translations WHERE video_id=? AND source='model'", (video_id,))
        conn.executemany(
            "INSERT INTO translations(video_id, idx, lang, source, text) VALUES(?,?,?,?,?)",
            [(video_id, seg.i, lang, "model", getattr(seg, lang)) for seg in enr.segments for lang in ("ko", "en")],
        )
        conn.execute("DELETE FROM glosses WHERE video_id=?", (video_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO glosses(video_id, surface_lc, surface, lemma, pos, level, gloss_ko, gloss_en, seg_idx) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(video_id, g.surface.lower(), g.surface, g.lemma, g.pos, g.level, g.ko, g.en,
              _seg_index(segments_de, g.surface)) for g in enr.glosses],
        )
        conn.execute("UPDATE videos SET cefr=?, cefr_source='model', topics_json=?, summary_ko=? WHERE id=?",
                     (enr.cefr, json.dumps(enr.topics, ensure_ascii=False), enr.summary_ko, video_id))
        _record(conn, video_id, backend_name, "ok", usage, enr.model_dump_json())


def enrich_one(conn: sqlite3.Connection, video_id: str, backend=None, force: bool = False) -> str:
    """Run the enrichment task for one video. Returns ok | fallback | failed | skipped."""
    backend = backend or get_backend()
    if backend.name == "none":
        return "skipped"
    row = conn.execute(
        "SELECT v.id, v.title, c.title AS channel_title, c.topics_json FROM videos v "
        "JOIN channels c ON c.id=v.channel_id WHERE v.id=?", (video_id,)).fetchone()
    if row is None:
        return "skipped"
    segments_de = [r[0] for r in conn.execute("SELECT text_de FROM segments WHERE video_id=? ORDER BY idx", (video_id,))]
    if not segments_de:
        return "skipped"
    if not force:
        prev = conn.execute("SELECT status, prompt_version, model FROM enrichments WHERE video_id=?", (video_id,)).fetchone()
        if prev and prev[0] == "ok" and prev[1] == PROMPT_VERSION and prev[2] == getattr(backend, "model", prev[2]):
            return "skipped"
    channel_topics = json.loads(row["topics_json"] or "[]")
    payload = build_payload(row["channel_title"], row["title"], segments_de)
    system_prompt = load_system_prompt()
    allowed = topic_ids()
    error_hint: str | None = None
    usage = Usage()
    for attempt in range(2):
        try:
            enr, usage_now = backend.enrich(payload, system_prompt, error_hint)
        except BackendError as e:
            log.warning("enrich %s: backend error: %s", video_id, e)
            with tx(conn):
                _record(conn, video_id, backend.name, "failed", usage, str(e))
            return "failed"
        usage.input_tokens += usage_now.input_tokens
        usage.output_tokens += usage_now.output_tokens
        usage.cost_usd += usage_now.cost_usd
        usage.latency_ms += usage_now.latency_ms
        usage.model = usage_now.model
        try:
            cleaned, issues = validate_enrichment(enr, segments_de, allowed, channel_topics)
        except EnrichmentValidationError as e:
            error_hint = str(e)
            log.info("enrich %s: validation failed (attempt %d): %s", video_id, attempt + 1, e)
            continue
        if issues:
            log.debug("enrich %s: %s", video_id, "; ".join(issues))
        store_enrichment(conn, video_id, cleaned, segments_de, backend.name, usage)
        return "ok"
    with tx(conn):
        _record(conn, video_id, backend.name, "fallback", usage, error_hint)
    return "fallback"


def enrich_candidates(conn: sqlite3.Connection, limit: int, retry_failed: bool = False) -> list[str]:
    statuses = "('pending')" if not retry_failed else "('pending','fallback','failed')"
    sql = (
        "SELECT v.id FROM videos v JOIN channels c ON c.id=v.channel_id "
        f"WHERE c.enabled=1 AND v.transcript_status='ok' AND v.enrich_status IN {statuses} "
        f"ORDER BY {LEVEL_ORDER_SQL}, v.published_at DESC LIMIT ?"
    )
    return [r[0] for r in conn.execute(sql, (limit,))]


def run_enrich(conn: sqlite3.Connection, limit: int | None = None, video_id: str | None = None,
               retry_failed: bool = False, force: bool = False, backend=None) -> dict[str, Any]:
    summary: dict[str, Any] = {"ok": 0, "fallback": 0, "failed": 0, "skipped": 0, "llm_calls": 0, "cost_usd": 0.0}
    backend = backend or get_backend()
    if backend.name == "none":
        log.info("enrich: backend disabled")
        return summary
    if not backend.is_ready():
        log.warning("enrich: backend %s not ready (%s)", backend.name, getattr(backend, "url", ""))
        summary["error"] = "backend not ready"
        return summary
    cap = settings.llm_daily_cap
    n = min(limit or cap, cap)
    ids = [video_id] if video_id else enrich_candidates(conn, n, retry_failed)
    for vid in ids:
        status = enrich_one(conn, vid, backend=backend, force=force)
        summary[status] = summary.get(status, 0) + 1
        if status != "skipped":
            summary["llm_calls"] += 1
            r = conn.execute("SELECT cost_usd FROM enrichments WHERE video_id=?", (vid,)).fetchone()
            summary["cost_usd"] += float(r[0] or 0) if r else 0.0
        log.info("enrich %s: %s", vid, status)
    return summary

"""Topic taxonomy loaded from data/topics.yaml (ids are stable; labels are Korean)."""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

from .config import ROOT

TOPICS_FILE = ROOT / "data" / "topics.yaml"


@lru_cache(maxsize=1)
def load_topics() -> list[dict[str, Any]]:
    data = yaml.safe_load(TOPICS_FILE.read_text(encoding="utf-8")) or {}
    topics = data.get("topics", [])
    for t in topics:
        t.setdefault("keywords", [])
    return topics


def topic_ids() -> list[str]:
    return [t["id"] for t in load_topics()]


def topic_labels() -> dict[str, str]:
    return {t["id"]: t.get("label_ko", t["id"]) for t in load_topics()}


def topic_keywords() -> dict[str, list[str]]:
    return {t["id"]: [k.lower() for k in t.get("keywords", [])] for t in load_topics()}


def match_topics(text: str, max_topics: int = 3) -> list[str]:
    """Cheap keyword matcher (no-LLM fallback for titles / Takeout history)."""
    low = (text or "").lower()
    scored: list[tuple[int, str]] = []
    for tid, kws in topic_keywords().items():
        hits = sum(1 for k in kws if k and k in low)
        if hits:
            scored.append((hits, tid))
    scored.sort(reverse=True)
    return [tid for _, tid in scored[:max_topics]]

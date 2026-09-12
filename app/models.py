"""Shared Pydantic models.

* Enrichment schema — the single JSON task performed by any LLM backend (Claude, the local
  fine-tuned student, or the teacher in ml/). pipeline/, app/ and ml/ all import it from here.
* validate_enrichment — the one validator every backend's output must pass.
* API DTOs used by the routers.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

CEFR = Literal["A1", "A2", "B1", "B2", "C1"]
CEFR_ORDER = ["A1", "A2", "B1", "B2", "C1"]
POS = Literal["noun", "verb", "adj", "adv", "other"]
GlossLevel = Literal["A2", "B1", "B2", "C1"]

PROMPT_VERSION = "enrich_v1"
MAX_GLOSSES = 12
MAX_TOPICS = 3


# ---------------------------------------------------------------------------
# Enrichment task schema
# ---------------------------------------------------------------------------
class Seg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    i: int = Field(description="segment index, 0-based, must match the input")
    de_clean: str = Field(description="German text with punctuation/casing fixed, words unchanged")
    ko: str = Field(description="natural Korean translation of this segment")
    en: str = Field(description="natural English translation of this segment")


class Gloss(BaseModel):
    model_config = ConfigDict(extra="forbid")
    surface: str = Field(description="the word exactly as it appears in the German text")
    lemma: str = Field(description="dictionary form; nouns as 'der Hund, -e'")
    pos: POS
    level: GlossLevel = Field(description="CEFR level of the word (only words above A1 are glossed)")
    ko: str = Field(description="short Korean meaning")
    en: str = Field(description="short English meaning")


class Enrichment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[Seg]
    glosses: list[Gloss] = Field(default_factory=list)
    cefr: CEFR = Field(description="overall difficulty of the whole video for a learner")
    topics: list[str] = Field(default_factory=list, description="1-3 topic ids from the taxonomy")
    summary_ko: str = Field(default="", description="one-line Korean summary of the video")


def enrichment_json_schema() -> dict[str, Any]:
    """JSON schema handed to llama-server / Ollama for grammar-constrained decoding."""
    return Enrichment.model_json_schema()


class EnrichmentValidationError(ValueError):
    """Fatal problems (wrong segment count/indices, empty translations)."""


_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s.strip().lower())


def validate_enrichment(
    enr: Enrichment,
    segments_de: list[str],
    allowed_topics: Iterable[str],
    channel_topics: Iterable[str] = (),
) -> tuple[Enrichment, list[str]]:
    """Validate and clean a backend's output against the German input segments.

    Raises EnrichmentValidationError for fatal mismatches (caller retries once, then falls
    back to YouTube MT). Returns (cleaned enrichment, list of non-fatal issues).
    """
    issues: list[str] = []
    n = len(segments_de)
    if len(enr.segments) != n:
        raise EnrichmentValidationError(f"segment count {len(enr.segments)} != expected {n}")
    idx = [s.i for s in enr.segments]
    if idx != list(range(n)):
        raise EnrichmentValidationError(f"segment indices {idx[:6]}... must be 0..{n - 1} in order")

    segs: list[Seg] = []
    for s in enr.segments:
        ko, en = s.ko.strip(), s.en.strip()
        if not ko or not en:
            raise EnrichmentValidationError(f"empty translation at segment {s.i}")
        de_clean = s.de_clean.strip() or segments_de[s.i]
        segs.append(Seg(i=s.i, de_clean=de_clean, ko=ko, en=en))

    full_text = _norm(" ".join(segments_de))
    seen_lemmas: set[str] = set()
    kept: list[Gloss] = []
    for g in enr.glosses:
        surface = g.surface.strip()
        if not surface or _norm(surface) not in full_text:
            issues.append(f"gloss '{surface}' not found in text; dropped")
            continue
        key = _norm(g.lemma) or _norm(surface)
        if key in seen_lemmas:
            continue
        seen_lemmas.add(key)
        kept.append(Gloss(surface=surface, lemma=g.lemma.strip() or surface, pos=g.pos,
                          level=g.level, ko=g.ko.strip(), en=g.en.strip()))
        if len(kept) >= MAX_GLOSSES:
            issues.append("gloss list truncated")
            break

    allowed = set(allowed_topics)
    topics = [t for t in enr.topics if t in allowed]
    if len(topics) != len(enr.topics):
        issues.append("unknown topics dropped")
    if not topics:
        topics = [t for t in channel_topics if t in allowed]
        if topics:
            issues.append("topics taken from channel")
    # de-duplicate, keep order
    topics = list(dict.fromkeys(topics))[:MAX_TOPICS]

    cleaned = Enrichment(segments=segs, glosses=kept, cefr=enr.cefr, topics=topics,
                         summary_ko=enr.summary_ko.strip())
    return cleaned, issues


# ---------------------------------------------------------------------------
# API DTOs
# ---------------------------------------------------------------------------
class ChannelBrief(BaseModel):
    id: str
    title: Optional[str] = None
    handle: Optional[str] = None


class FeedItem(BaseModel):
    video_id: str
    channel: ChannelBrief
    title: Optional[str] = None
    duration_s: Optional[int] = None
    cefr: Optional[str] = None
    topics: list[str] = Field(default_factory=list)
    has_dub: int = 0
    pair_video_id: Optional[str] = None
    summary_ko: Optional[str] = None
    has_model: bool = False
    youtube_url: str


class FeedOut(BaseModel):
    items: list[FeedItem]


class SegmentOut(BaseModel):
    idx: int
    start_ms: int
    end_ms: int
    de: str
    de_clean: Optional[str] = None
    tr: Optional[str] = None


class GlossOut(BaseModel):
    surface: str
    lemma: str
    pos: str
    level: Optional[str] = None
    ko: Optional[str] = None
    en: Optional[str] = None
    seg_idx: Optional[int] = None


class SubtitlesOut(BaseModel):
    video_id: str
    lang: str
    tr_source: Optional[str] = None  # model | yt_mt | user | None
    segments: list[SegmentOut]
    glosses: list[GlossOut] = Field(default_factory=list)


EventType = Literal[
    "impression", "play", "watch", "complete", "like", "skip", "save_word",
    "embed_error", "too_hard", "too_easy", "no_dub",
]


class EventIn(BaseModel):
    video_id: str
    type: EventType
    value: Optional[float] = None
    mode: Optional[str] = None
    position_ms: Optional[int] = None


class CorrectionIn(BaseModel):
    video_id: str
    seg_idx: Optional[int] = None
    kind: Literal["translation", "gloss", "level"]
    before: Optional[dict[str, Any]] = None
    after: dict[str, Any]


class VocabIn(BaseModel):
    lemma: str
    surface: Optional[str] = None
    pos: Optional[str] = None
    gloss_ko: Optional[str] = None
    gloss_en: Optional[str] = None
    video_id: Optional[str] = None
    seg_idx: Optional[int] = None
    sentence_de: Optional[str] = None
    sentence_ko: Optional[str] = None


class VocabOut(VocabIn):
    id: int
    created_at: str
    exported_at: Optional[str] = None


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="allow")
    subtitle_lang: Optional[Literal["ko", "en"]] = None
    mode: Optional[Literal["listening", "reading"]] = None
    stretch: Optional[bool] = None
    playback_rate: Optional[float] = None
    reveal_de_on_tap: Optional[bool] = None
    prefer_dub: Optional[bool] = None
    llm_enabled: Optional[bool] = None
    onboarded: Optional[bool] = None
    topics: Optional[list[str]] = None  # onboarding: chosen interest topics


class TopicOut(BaseModel):
    id: str
    label_ko: str


class ChannelOut(BaseModel):
    id: str
    handle: Optional[str] = None
    title: Optional[str] = None
    level_hint: Optional[str] = None
    topics: list[str] = Field(default_factory=list)
    enabled: bool = True
    video_count: int = 0


class HealthOut(BaseModel):
    ok: bool = True
    version: str
    llm_backend: str
    llm_ready: bool
    counts: dict[str, int]

"""Shared helpers for the ml/ track (pure Python; safe to import on the PC).

Every script does `from common import ...` after this module bootstraps sys.path so that
`app.models` (the Enrichment schema + validator) and `app.taxonomy` import from the repo root.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable

ML_DIR = Path(__file__).resolve().parent
ROOT = ML_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import PROMPT_VERSION, Enrichment, EnrichmentValidationError, validate_enrichment  # noqa: E402
from app.taxonomy import topic_ids  # noqa: E402

DATA = ML_DIR / "data"
RAW = DATA / "raw"
WORK = DATA / "work"
SFT = DATA / "sft"
GOLD = DATA / "gold"
FEEDBACK = DATA / "feedback"
RUNS = ML_DIR / "runs"
PROMPT_FILE = ROOT / "pipeline" / "prompts" / "enrich_v1.md"

TRANSLATE_PROMPTS = {
    "ko": "Translate the German sentence into natural Korean. Output only the translation.",
    "en": "Translate the German sentence into natural English. Output only the translation.",
}
GLOSS_PROMPT = ("You are a German teacher. For the given German sentence and the marked word, answer with a JSON object "
                "{\"lemma\", \"pos\", \"level\", \"ko\", \"en\"}: dictionary form (nouns as 'der Hund, -e'), part of speech "
                "(noun/verb/adj/adv/other), CEFR level (A2/B1/B2/C1), and short Korean and English meanings for this context. JSON only.")
CEFR_PROMPT = ("Rate the CEFR level (A1, A2, B1, B2 or C1) a learner needs to follow this German transcript comfortably. "
               "Answer with the level only.")


def ensure_dirs() -> None:
    for d in (RAW, WORK, SFT, GOLD, FEEDBACK, RUNS):
        d.mkdir(parents=True, exist_ok=True)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def iter_jsonl(path: str | Path) -> Iterable[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]], append: bool = False) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("a" if append else "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def load_yaml(path: str | Path) -> dict[str, Any]:
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def system_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8").replace("{topics}", ", ".join(topic_ids()))


def payload_for(video: dict[str, Any]) -> dict[str, Any]:
    return {"channel": video.get("channel_title") or video.get("channel_handle") or "",
            "title": video.get("title") or "",
            "segments": [{"i": i, "de": s["de"]} for i, s in enumerate(video["segments"])]}


def enrichment_to_json(enr: Enrichment | dict[str, Any]) -> str:
    data = enr.model_dump() if isinstance(enr, Enrichment) else enr
    return json.dumps(data, ensure_ascii=False)


def chat_example(video: dict[str, Any], enrichment: Enrichment | dict[str, Any], task: str = "enrich") -> dict[str, Any]:
    """One SFT example in chat format; identical to what the app sends the local backend."""
    return {
        "task": task,
        "video_id": video.get("video_id"),
        "channel_id": video.get("channel_id"),
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": json.dumps(payload_for(video), ensure_ascii=False)},
            {"role": "assistant", "content": enrichment_to_json(enrichment)},
        ],
    }


def stable_hash(s: str) -> float:
    """Deterministic float in [0, 1) from a string (sha1)."""
    h = hashlib.sha1(s.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") / float(1 << 64)


def is_heldout_channel(channel_id: str, fraction: float) -> bool:
    return stable_hash("channel:" + (channel_id or "")) < fraction


def video_split(video_id: str, channel_id: str, heldout_fraction: float, val_fraction: float,
                force_test: bool = False) -> str:
    """train | val | test. Whole channels are held out for test; val is a slice of the rest by video hash."""
    if force_test or is_heldout_channel(channel_id, heldout_fraction):
        return "test"
    return "val" if stable_hash("video:" + video_id) < val_fraction else "train"


def sentence_split(text: str, val_fraction: float, test_fraction: float) -> str:
    h = stable_hash("sent:" + text)
    if h < test_fraction:
        return "test"
    if h < test_fraction + val_fraction:
        return "val"
    return "train"

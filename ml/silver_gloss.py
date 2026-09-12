"""Server side: silver gloss candidates from spaCy + Wiktionary (kaikki.org) + frequency ranks.

    python -m spacy download de_core_news_md
    curl -L -o ml/data/raw/kaikki-de.jsonl https://kaikki.org/dictionary/German/kaikki.org-dictionary-German.jsonl
    python ml/silver_gloss.py --wiktionary ml/data/raw/kaikki-de.jsonl [--max-per-video 12]

Output ml/data/work/silver_gloss.jsonl: {video_id, seg_idx, surface, lemma, pos, level, en, sentence}
Korean meanings are added later by `teacher_label.py --gloss-ko`.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from common import ROOT, WORK, ensure_dirs, iter_jsonl, read_jsonl, write_jsonl  # noqa: E402

POS_MAP = {"NOUN": "noun", "PROPN": None, "VERB": "verb", "AUX": "verb", "ADJ": "adj", "ADV": "adv"}
RANK_BANDS = [(1000, "A1"), (2500, "A2"), (5000, "B1"), (10000, "B2")]


def load_ranks() -> dict[str, int]:
    ranks: dict[str, int] = {}
    with (ROOT / "data" / "de_50k.txt").open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            w = line.split(" ", 1)[0].lower()
            ranks.setdefault(w, i)
    return ranks


def level_for(word: str, ranks: dict[str, int]) -> str:
    r = ranks.get(word.lower())
    if r is None:
        return "C1"
    for limit, lvl in RANK_BANDS:
        if r <= limit:
            return lvl
    return "C1"


def load_wiktionary(path: Path) -> dict[tuple[str, str], dict]:
    """(lemma_lower, pos) -> {gloss_en, gender, plural}. kaikki lines: word, pos, senses[].glosses, forms[]."""
    out: dict[tuple[str, str], dict] = {}
    pos_norm = {"noun": "noun", "verb": "verb", "adj": "adj", "adv": "adv"}
    for e in iter_jsonl(path):
        pos = pos_norm.get(e.get("pos", ""))
        if not pos:
            continue
        key = (str(e.get("word", "")).lower(), pos)
        if key in out:
            continue
        glosses = [g for s in e.get("senses", []) for g in s.get("glosses", []) if g]
        if not glosses:
            continue
        gender = None
        for tag in e.get("tags", []) or []:
            if tag in ("masculine", "feminine", "neuter"):
                gender = tag
        if gender is None:
            for s in e.get("senses", []):
                for tag in s.get("tags", []) or []:
                    if tag in ("masculine", "feminine", "neuter"):
                        gender = tag
        plural = None
        for f in e.get("forms", []) or []:
            if "plural" in (f.get("tags") or []) and "nominative" in (f.get("tags") or []):
                plural = f.get("form")
                break
        out[key] = {"gloss_en": glosses[0][:80], "gender": gender, "plural": plural}
    return out


def noun_lemma(word: str, info: dict) -> str:
    art = {"masculine": "der", "feminine": "die", "neuter": "das"}.get(info.get("gender") or "", "")
    base = f"{art} {word}".strip()
    plural = info.get("plural")
    if plural and plural.startswith(word):
        return f"{base}, -{plural[len(word):]}" if len(plural) > len(word) else f"{base}, -"
    return f"{base}, {plural}" if plural else base


def run(wiktionary: Path | None, max_per_video: int) -> int:
    import spacy  # heavy import kept local
    nlp = spacy.load("de_core_news_md", disable=["ner", "parser"])
    ranks = load_ranks()
    wik = load_wiktionary(wiktionary) if wiktionary else {}
    rows = []
    for v in iter_jsonl(WORK / "transcripts.jsonl"):
        seen: set[str] = set()
        cands = []
        for seg in v["segments"]:
            doc = nlp(seg["de"])
            for tok in doc:
                pos = POS_MAP.get(tok.pos_)
                if not pos or not tok.is_alpha or len(tok.text) < 3:
                    continue
                lemma = tok.lemma_
                level = level_for(lemma, ranks)
                if level == "A1" or lemma.lower() in seen:
                    continue
                seen.add(lemma.lower())
                info = wik.get((lemma.lower(), pos), {})
                full_lemma = noun_lemma(lemma, info) if pos == "noun" else lemma
                rank = ranks.get(lemma.lower(), 99999)
                cands.append((rank, {"video_id": v["video_id"], "seg_idx": seg["i"], "surface": tok.text, "lemma": full_lemma,
                                     "pos": pos, "level": level, "en": info.get("gloss_en"), "sentence": seg["de"]}))
        cands.sort(key=lambda c: c[0])           # most frequent (most useful) first
        rows.extend(c for _, c in cands[:max_per_video])
    return write_jsonl(WORK / "silver_gloss.jsonl", rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wiktionary", type=Path, default=None)
    ap.add_argument("--max-per-video", type=int, default=12)
    a = ap.parse_args()
    ensure_dirs()
    print("silver glosses:", run(a.wiktionary, a.max_per_video))


if __name__ == "__main__":
    main()

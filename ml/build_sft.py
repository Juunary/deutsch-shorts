"""Assemble SFT data (chat format) -> ml/data/sft/{train,val,test}.jsonl + stats.json.

    python ml/build_sft.py [--config ml/configs/student_v1.yaml] [--seed 13]

Tasks: enrich (full JSON task from teacher labels; user corrections override; gold videos -> test),
       translate (de->ko/en sentence pairs), gloss (silver glosses), cefr (transcript -> level).
Splits are by VIDEO: whole channels held out for test (deterministic hash), val = slice of the rest.
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from typing import Any

from common import (CEFR_PROMPT, GLOSS_PROMPT, GOLD, ML_DIR, SFT, TRANSLATE_PROMPTS, WORK, chat_example,  # noqa: E402
                    ensure_dirs, load_yaml, read_jsonl, video_split, write_jsonl)


def apply_corrections(label: dict[str, Any], video: dict[str, Any]) -> dict[str, Any]:
    """User corrections (translation / gloss / level) override the teacher's label."""
    enr = json.loads(json.dumps(label["enrichment"]))
    for c in video.get("corrections") or []:
        after = c.get("after") or {}
        if c["kind"] == "translation" and c.get("seg_idx") is not None:
            lang = after.get("lang", "ko")
            for s in enr["segments"]:
                if s["i"] == c["seg_idx"] and after.get("text"):
                    s[lang] = after["text"]
        elif c["kind"] == "gloss" and after.get("wrong"):
            enr["glosses"] = [g for g in enr["glosses"] if g["surface"].lower() != str(after.get("surface", "")).lower()]
        elif c["kind"] == "level" and after.get("cefr"):
            enr["cefr"] = after["cefr"]
    return enr


def build(config: dict[str, Any], seed: int = 13) -> dict[str, Any]:
    ensure_dirs()
    rng = random.Random(seed)
    data_cfg = config.get("data", {})
    heldout = float(data_cfg.get("heldout_channel_fraction", 0.2))
    val_frac = float(data_cfg.get("val_fraction", 0.05))
    mix = data_cfg.get("mix", {"enrich": 1.0, "translate": 0.4, "gloss": 0.2, "cefr": 0.1})
    caps = data_cfg.get("max_examples", {})

    videos = {v["video_id"]: v for v in read_jsonl(WORK / "transcripts.jsonl")}
    labels = read_jsonl(WORK / "teacher_labels.jsonl")
    labelled = {lab["video_id"] for lab in labels}
    for v in videos.values():  # enrichments produced by `python -m pipeline enrich` (teacher via LocalBackend) count as labels too
        if v.get("enrichment") and v["video_id"] not in labelled:
            labels.append({"video_id": v["video_id"], "enrichment": v["enrichment"], "model": v.get("enrich_model")})
            labelled.add(v["video_id"])
    gold_ids = {r["video_id"] for p in GOLD.glob("*.jsonl") for r in read_jsonl(p)}
    splits: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}
    stats: Counter = Counter()

    # (a) enrich
    for lab in labels:
        v = videos.get(lab["video_id"])
        if not v:
            continue
        force_test = v.get("gold") or lab["video_id"] in gold_ids
        split = video_split(lab["video_id"], v["channel_id"], heldout, val_frac, force_test=bool(force_test))
        ex = chat_example(v, apply_corrections(lab, v), task="enrich")
        ex["split"] = split
        splits[split].append(ex)
        stats[f"enrich/{split}"] += 1
    n_enrich = stats["enrich/train"]

    # (b) translate
    for lang in ("ko", "en"):
        rows = read_jsonl(WORK / f"translate_de_{lang}.jsonl")
        rng.shuffle(rows)
        cap = int(min(caps.get("translate", 10 ** 9), max(1, mix.get("translate", 0.4) * max(n_enrich, 1) * 40)))
        for r in rows[:cap]:
            split = r.get("split", "train")
            splits[split].append({"task": f"translate_{lang}", "split": split, "messages": [
                {"role": "system", "content": TRANSLATE_PROMPTS[lang]}, {"role": "user", "content": r["de"]},
                {"role": "assistant", "content": r[lang]}]})
            stats[f"translate_{lang}/{split}"] += 1

    # (c) gloss
    glosses = [g for g in read_jsonl(WORK / "silver_gloss.jsonl") if g.get("ko") and g.get("en")]
    rng.shuffle(glosses)
    for g in glosses[: int(caps.get("gloss", 30000))]:
        v = videos.get(g["video_id"])
        split = video_split(g["video_id"], v["channel_id"] if v else "", heldout, val_frac)
        target = {"lemma": g["lemma"], "pos": g["pos"], "level": g["level"], "ko": g["ko"], "en": g["en"]}
        splits[split].append({"task": "gloss", "split": split, "messages": [
            {"role": "system", "content": GLOSS_PROMPT},
            {"role": "user", "content": json.dumps({"sentence": g["sentence"], "word": g["surface"]}, ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)}]})
        stats[f"gloss/{split}"] += 1

    # (d) cefr
    for lab in labels[: int(caps.get("cefr", 5000))]:
        v = videos.get(lab["video_id"])
        if not v:
            continue
        split = video_split(lab["video_id"], v["channel_id"], heldout, val_frac)
        splits[split].append({"task": "cefr", "split": split, "messages": [
            {"role": "system", "content": CEFR_PROMPT},
            {"role": "user", "content": " ".join(s["de"] for s in v["segments"])},
            {"role": "assistant", "content": lab["enrichment"]["cefr"]}]})
        stats[f"cefr/{split}"] += 1

    for name, rows in splits.items():
        rng.shuffle(rows)
        write_jsonl(SFT / f"{name}.jsonl", rows)
    out = {"counts": dict(stats), "sizes": {k: len(v) for k, v in splits.items()}, "config": data_cfg, "seed": seed}
    (SFT / "stats.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ML_DIR / "configs" / "student_v1.yaml"))
    ap.add_argument("--seed", type=int, default=13)
    a = ap.parse_args()
    out = build(load_yaml(a.config), a.seed)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

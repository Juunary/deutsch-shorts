"""Evaluate a system on ml/data/sft/test.jsonl (enrich task) and ml/data/gold/*.jsonl.

    python ml/eval.py --name student_v1 --url http://127.0.0.1:8081 --model student-v1
    python ml/eval.py --name teacher --labels ml/data/work/teacher_labels.jsonl
    python ml/eval.py --name yt_mt --system mt            # machine translation baseline from transcripts.jsonl

Writes ml/runs/eval_<name>.json and appends a row to ml/runs/results.md.
Metrics: JSON validity, validator pass rate, segment alignment, chrF++ ko/en (pure Python; sacrebleu if importable),
gloss P/R/F1 vs reference, CEFR exact/adjacent accuracy, mean latency.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from common import (Enrichment, EnrichmentValidationError, GOLD, RUNS, SFT, WORK, ensure_dirs, read_jsonl,  # noqa: E402
                    topic_ids, validate_enrichment)
from metrics import alignment_accuracy, cefr_accuracy, chrf, corpus_chrf, gloss_prf  # noqa: E402


def reference_items() -> list[dict[str, Any]]:
    """Test examples: {video_id, channel_id, messages, ref: Enrichment dict}."""
    items = []
    for ex in read_jsonl(SFT / "test.jsonl"):
        if ex.get("task") != "enrich":
            continue
        items.append({"video_id": ex["video_id"], "channel_id": ex.get("channel_id"), "messages": ex["messages"],
                      "ref": json.loads(ex["messages"][2]["content"]), "gold": False})
    for p in GOLD.glob("*.jsonl"):
        for r in read_jsonl(p):
            items.append({"video_id": r["video_id"], "channel_id": r.get("channel_id"), "messages": r.get("messages"),
                          "ref": r["enrichment"], "gold": True})
    return items


def predict_endpoint(url: str, model: str, messages: list[dict[str, str]], timeout: float = 300) -> tuple[str, int]:
    import httpx
    t0 = time.perf_counter()
    r = httpx.post(f"{url.rstrip('/')}/v1/chat/completions", timeout=timeout, json={
        "model": model, "messages": messages[:2], "temperature": 0.2, "max_tokens": 4096,
        "response_format": {"type": "json_schema", "json_schema": {"name": "enrichment", "schema": Enrichment.model_json_schema(), "strict": True}}})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"], int((time.perf_counter() - t0) * 1000)


def evaluate(name: str, predictions: dict[str, tuple[str | None, int]], items: list[dict[str, Any]]) -> dict[str, Any]:
    segments_de = {v["video_id"]: [s["de"] for s in v["segments"]] for v in read_jsonl(WORK / "transcripts.jsonl")}
    valid = passed = 0
    pred_counts, ref_counts, hyp_ko, ref_ko, hyp_en, ref_en, cefr_p, cefr_r, prfs, lat = [], [], [], [], [], [], [], [], [], []
    for it in items:
        text, ms = predictions.get(it["video_id"], (None, 0))
        ref = it["ref"]
        ref_counts.append(len(ref["segments"]))
        lat.append(ms)
        enr = None
        if text:
            try:
                enr = Enrichment.model_validate_json(text)
                valid += 1
            except Exception:  # noqa: BLE001
                enr = None
        if enr is None:
            pred_counts.append(None)
            cefr_p.append(None)
            prfs.append((0.0, 0.0, 0.0))
            continue
        pred_counts.append(len(enr.segments))
        de = segments_de.get(it["video_id"]) or [s["de_clean"] for s in ref["segments"]]
        try:
            cleaned, _ = validate_enrichment(enr, de, topic_ids())
            passed += 1
        except EnrichmentValidationError:
            cleaned = enr
        for s, r in zip(cleaned.segments, ref["segments"]):
            hyp_ko.append(s.ko); ref_ko.append(r["ko"]); hyp_en.append(s.en); ref_en.append(r["en"])
        cefr_p.append(cleaned.cefr)
        prfs.append(gloss_prf([g.lemma for g in cleaned.glosses], [g["lemma"] for g in ref["glosses"]]))
    for it in items:
        cefr_r.append(it["ref"]["cefr"])
    n = max(1, len(items))
    exact, adjacent = cefr_accuracy(cefr_p, cefr_r)
    res = {
        "name": name, "n": len(items), "json_valid": valid / n, "validator_pass": passed / n,
        "alignment_acc": alignment_accuracy(pred_counts, ref_counts),
        "chrf_ko": corpus_chrf(hyp_ko, ref_ko), "chrf_en": corpus_chrf(hyp_en, ref_en),
        "gloss_p": sum(p[0] for p in prfs) / n, "gloss_r": sum(p[1] for p in prfs) / n, "gloss_f1": sum(p[2] for p in prfs) / n,
        "cefr_exact": exact, "cefr_adjacent": adjacent, "latency_ms": sum(lat) / n,
    }
    try:  # official chrF++ when sacrebleu is installed (server)
        import sacrebleu
        res["sacrebleu_chrf_ko"] = sacrebleu.corpus_chrf(hyp_ko, [ref_ko], word_order=2).score if hyp_ko else 0.0
        res["sacrebleu_chrf_en"] = sacrebleu.corpus_chrf(hyp_en, [ref_en], word_order=2).score if hyp_en else 0.0
    except Exception:  # noqa: BLE001
        pass
    return res


def mt_predictions(items: list[dict[str, Any]]) -> dict[str, tuple[str | None, int]]:
    """Baseline: machine translations from transcripts.jsonl wrapped into the Enrichment shape."""
    videos = {v["video_id"]: v for v in read_jsonl(WORK / "transcripts.jsonl")}
    out = {}
    for it in items:
        v = videos.get(it["video_id"])
        if not v or not v.get("mt_ko"):
            continue
        segs = [{"i": i, "de_clean": s["de"], "ko": (v["mt_ko"][i] or ""), "en": ((v.get("mt_en") or [None] * len(v["segments"]))[i] or "")}
                for i, s in enumerate(v["segments"])]
        out[it["video_id"]] = (json.dumps({"segments": segs, "glosses": [], "cefr": v.get("cefr") or "B1", "topics": [], "summary_ko": ""}, ensure_ascii=False), 0)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--url"); ap.add_argument("--model")
    ap.add_argument("--labels", help="jsonl with {video_id, enrichment} (e.g. teacher labels)")
    ap.add_argument("--system", choices=["mt"], help="built-in baseline")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    ensure_dirs()
    items = reference_items()
    if a.limit:
        items = items[:a.limit]
    preds: dict[str, tuple[str | None, int]] = {}
    if a.url:
        for i, it in enumerate(items):
            try:
                preds[it["video_id"]] = predict_endpoint(a.url, a.model or "student-v1", it["messages"])
            except Exception as e:  # noqa: BLE001
                print("predict failed", it["video_id"], e)
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(items)}")
    elif a.labels:
        for r in read_jsonl(a.labels):
            preds[r["video_id"]] = (json.dumps(r["enrichment"], ensure_ascii=False), int(r.get("latency_ms", 0)))
    elif a.system == "mt":
        preds = mt_predictions(items)
    res = evaluate(a.name, preds, items)
    (RUNS / f"eval_{a.name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    header = "| name | n | json | valid | align | chrF ko | chrF en | gloss F1 | cefr exact | cefr adj | ms |\n|---|---|---|---|---|---|---|---|---|---|---|\n"
    row = (f"| {res['name']} | {res['n']} | {res['json_valid']:.2f} | {res['validator_pass']:.2f} | {res['alignment_acc']:.2f} | "
           f"{res['chrf_ko']:.1f} | {res['chrf_en']:.1f} | {res['gloss_f1']:.2f} | {res['cefr_exact']:.2f} | {res['cefr_adjacent']:.2f} | {res['latency_ms']:.0f} |\n")
    results = RUNS / "results.md"
    if not results.exists():
        results.write_text(header, encoding="utf-8")
    with results.open("a", encoding="utf-8") as f:
        f.write(row)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

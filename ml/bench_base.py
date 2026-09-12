"""Server side: zero-shot benchmark of candidate base models on ~20 transcripts (JSON validity, validator pass, latency).

Serve each candidate (vLLM or llama-server, OpenAI-compatible) and run:
    python ml/bench_base.py --url http://127.0.0.1:8000 --model Qwen/Qwen3-4B --n 20
Appends a row to ml/runs/bench_base.md. Pick the model with the best validator pass rate and acceptable latency.
"""
from __future__ import annotations

import argparse
import json
import time

from common import (Enrichment, EnrichmentValidationError, RUNS, WORK, ensure_dirs, payload_for, read_jsonl,  # noqa: E402
                    system_prompt, topic_ids, validate_enrichment)


def main() -> None:
    import httpx
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--no-schema", action="store_true", help="do not send response_format (plain JSON prompting)")
    a = ap.parse_args()
    ensure_dirs()
    videos = read_jsonl(WORK / "transcripts.jsonl")[: a.n]
    sys_prompt = system_prompt()
    schema = Enrichment.model_json_schema()
    valid = passed = 0
    lat = []
    with httpx.Client(base_url=a.url.rstrip("/"), timeout=600) as c:
        for v in videos:
            body = {"model": a.model, "temperature": 0.2, "max_tokens": 4096,
                    "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": json.dumps(payload_for(v), ensure_ascii=False)}]}
            if not a.no_schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": "enrichment", "schema": schema}}
            t0 = time.perf_counter()
            try:
                r = c.post("/v1/chat/completions", json=body)
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"]
            except Exception as e:  # noqa: BLE001
                print("request failed:", e)
                lat.append(time.perf_counter() - t0)
                continue
            lat.append(time.perf_counter() - t0)
            try:
                enr = Enrichment.model_validate_json(text)
                valid += 1
            except Exception:  # noqa: BLE001
                continue
            try:
                validate_enrichment(enr, [s["de"] for s in v["segments"]], topic_ids(), v.get("channel_topics") or [])
                passed += 1
            except EnrichmentValidationError:
                pass
    n = max(1, len(videos))
    row = f"| {a.model} | {len(videos)} | {valid / n:.2f} | {passed / n:.2f} | {sum(lat) / max(1, len(lat)):.1f} s | schema={'off' if a.no_schema else 'on'} |\n"
    out = RUNS / "bench_base.md"
    if not out.exists():
        out.write_text("| model | n | json valid | validator pass | latency | note |\n|---|---|---|---|---|---|\n", encoding="utf-8")
    with out.open("a", encoding="utf-8") as f:
        f.write(row)
    print(row)


if __name__ == "__main__":
    main()

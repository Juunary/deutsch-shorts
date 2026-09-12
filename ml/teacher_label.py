"""Server side: label transcripts with the teacher model -> ml/data/work/teacher_labels.jsonl (resumable).

    python ml/teacher_label.py --backend vllm --limit 2000 --concurrency 4
    python ml/teacher_label.py --backend claude --limit 300        # only if enabled in configs/teacher.yaml
    python ml/teacher_label.py --gloss-ko                           # fill Korean meanings for silver glosses

The teacher gets the SAME system prompt / payload / JSON schema / validator as the app's local backend,
so its labels are exactly the target behaviour of the student.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from common import (ML_DIR, WORK, Enrichment, EnrichmentValidationError, PROMPT_VERSION, ensure_dirs,  # noqa: E402
                    iter_jsonl, payload_for, read_jsonl, system_prompt, topic_ids, validate_enrichment, write_jsonl)

CONFIG = ML_DIR / "configs" / "teacher.yaml"


class Teacher:
    def __init__(self, backend: str, cfg: dict[str, Any]) -> None:
        self.backend = backend
        self.cfg = cfg
        self.system = system_prompt()
        self.schema = Enrichment.model_json_schema()
        if backend == "vllm":
            import httpx
            self.client = httpx.Client(base_url=cfg["vllm"]["base_url"], timeout=600)
            self.model = cfg["vllm"]["model"]
            self.temperature = float(cfg["vllm"].get("temperature", 0.2))
        elif backend == "claude":
            if not cfg.get("claude", {}).get("enabled"):
                raise SystemExit("claude teacher is disabled in configs/teacher.yaml (set enabled: true after approving the cost)")
            import anthropic
            self.client = anthropic.Anthropic()
            self.model = cfg["claude"]["model"]
        else:
            raise SystemExit(f"unknown backend {backend}")

    def complete_json(self, user: str, schema: dict[str, Any], model_cls):
        if self.backend == "vllm":
            r = self.client.post("/chat/completions", json={
                "model": self.model, "temperature": self.temperature, "max_tokens": 4096,
                "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}},
            })
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return model_cls.model_validate_json(text), {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")}
        resp = self.client.messages.parse(model=self.model, max_tokens=8000, output_config={"effort": "low"},
                                          system=self.system, messages=[{"role": "user", "content": user}], output_format=model_cls)
        if getattr(resp, "stop_reason", None) == "refusal":
            raise RuntimeError("refusal")
        return resp.parsed_output, {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}

    def label(self, video: dict[str, Any]) -> dict[str, Any]:
        payload = payload_for(video)
        segments_de = [s["de"] for s in video["segments"]]
        user = json.dumps(payload, ensure_ascii=False)
        t0 = time.perf_counter()
        hint = None
        issues: list[str] = []
        for attempt in range(2):
            enr, usage = self.complete_json(user + (f"\n\nYour previous answer was rejected: {hint}. Return a corrected JSON object." if hint else ""),
                                            self.schema, Enrichment)
            try:
                cleaned, issues = validate_enrichment(enr, segments_de, topic_ids(), video.get("channel_topics") or [])
            except EnrichmentValidationError as e:
                hint = str(e)
                continue
            return {"video_id": video["video_id"], "channel_id": video["channel_id"], "model": self.model, "backend": self.backend,
                    "prompt_version": PROMPT_VERSION, "enrichment": cleaned.model_dump(), "issues": issues,
                    "latency_ms": int((time.perf_counter() - t0) * 1000), "usage": usage, "attempts": attempt + 1}
        raise EnrichmentValidationError(hint or "invalid")


def label_all(backend: str, limit: int | None, concurrency: int) -> None:
    from common import load_yaml
    cfg = load_yaml(CONFIG)
    teacher = Teacher(backend, cfg)
    out = WORK / "teacher_labels.jsonl"
    fails = WORK / "teacher_failures.jsonl"
    done = {r["video_id"] for r in read_jsonl(out)}
    failed = {r["video_id"] for r in read_jsonl(fails)}
    todo = [v for v in iter_jsonl(WORK / "transcripts.jsonl") if v["video_id"] not in done and v["video_id"] not in failed]
    if backend == "claude":
        todo = todo[: int(cfg["claude"].get("max_videos", 500))]
    if limit:
        todo = todo[:limit]
    print(f"labeling {len(todo)} videos with {backend} ({len(done)} done, {len(failed)} failed before)")
    ok = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex, out.open("a", encoding="utf-8") as fo, fails.open("a", encoding="utf-8") as ff:
        futures = {ex.submit(teacher.label, v): v for v in todo}
        for fut in as_completed(futures):
            v = futures[fut]
            try:
                row = fut.result()
                fo.write(json.dumps(row, ensure_ascii=False) + "\n")
                fo.flush()
                ok += 1
                if ok % 25 == 0:
                    print(f"  {ok}/{len(todo)}")
            except Exception as e:  # noqa: BLE001
                ff.write(json.dumps({"video_id": v["video_id"], "error": f"{type(e).__name__}: {str(e)[:200]}"}, ensure_ascii=False) + "\n")
                ff.flush()
    print(f"done: {ok} ok, {len(todo) - ok} failed")


GLOSS_KO_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "ko": {"type": "string"}}, "required": ["i", "ko"]}}},
    "required": ["items"]}


def gloss_ko(backend: str, batch: int = 40) -> None:
    """Fill Korean meanings for silver glosses (from lemma + English gloss + sentence)."""
    from pydantic import BaseModel
    from common import load_yaml

    class Item(BaseModel):
        i: int
        ko: str

    class Out(BaseModel):
        items: list[Item]

    cfg = load_yaml(CONFIG)
    teacher = Teacher(backend, cfg)
    teacher.system = ("You are a German-Korean dictionary. For each item give a short Korean meaning (2-6 words) of the German word "
                      "as used in its sentence, using the English gloss as a hint. Answer as JSON {\"items\":[{\"i\", \"ko\"}]}.")
    rows = read_jsonl(WORK / "silver_gloss.jsonl")
    todo = [(i, r) for i, r in enumerate(rows) if not r.get("ko")]
    print(f"{len(todo)} glosses need Korean")
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        user = json.dumps([{"i": i, "word": r["lemma"], "en": r.get("en"), "sentence": r["sentence"]} for i, r in chunk], ensure_ascii=False)
        try:
            out, _ = teacher.complete_json(user, GLOSS_KO_SCHEMA, Out)
        except Exception as e:  # noqa: BLE001
            print("batch failed:", e)
            continue
        by_i = {it.i: it.ko for it in out.items}
        for i, r in chunk:
            if by_i.get(i):
                r["ko"] = by_i[i]
    write_jsonl(WORK / "silver_gloss.jsonl", rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="vllm", choices=["vllm", "claude"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--gloss-ko", action="store_true")
    a = ap.parse_args()
    ensure_dirs()
    if a.gloss_ko:
        gloss_ko(a.backend)
    else:
        label_all(a.backend, a.limit, a.concurrency)


if __name__ == "__main__":
    main()

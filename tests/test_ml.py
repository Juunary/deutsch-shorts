from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ML = Path(__file__).resolve().parent.parent / "ml"
if str(ML) not in sys.path:
    sys.path.insert(0, str(ML))

import common as C  # noqa: E402
import metrics as M  # noqa: E402


def test_split_is_deterministic_and_exclusive():
    a = C.video_split("v1", "UCa", 0.2, 0.05)
    assert a == C.video_split("v1", "UCa", 0.2, 0.05)
    assert C.video_split("v1", "UCa", 0.2, 0.05, force_test=True) == "test"
    heldout = [c for c in (f"UC{i}" for i in range(200)) if C.is_heldout_channel(c, 0.2)]
    assert 20 <= len(heldout) <= 60
    for c in heldout:
        assert C.video_split("x", c, 0.2, 0.05) == "test"
    splits = {C.video_split(f"vid{i}", "UCkeep", 0.0, 0.1) for i in range(300)}
    assert splits == {"train", "val"}


def test_chat_example_roundtrip():
    video = {"video_id": "v", "channel_id": "c", "channel_title": "Easy German", "title": "T", "segments": [{"i": 0, "de": "hallo welt"}]}
    enr = {"segments": [{"i": 0, "de_clean": "Hallo Welt", "ko": "안녕 세상", "en": "hello world"}], "glosses": [], "cefr": "A1", "topics": ["daily_life"], "summary_ko": "s"}
    ex = C.chat_example(video, enr)
    assert [m["role"] for m in ex["messages"]] == ["system", "user", "assistant"]
    assert "daily_life" in ex["messages"][0]["content"] and json.loads(ex["messages"][1]["content"])["segments"][0]["de"] == "hallo welt"
    assert C.Enrichment.model_validate_json(ex["messages"][2]["content"]).segments[0].ko == "안녕 세상"


def test_build_sft_from_fixtures(tmp_path, monkeypatch):
    import build_sft as B
    monkeypatch.setattr(C, "WORK", tmp_path / "work"); monkeypatch.setattr(C, "SFT", tmp_path / "sft"); monkeypatch.setattr(C, "GOLD", tmp_path / "gold")
    for name in ("WORK", "SFT", "GOLD"):
        monkeypatch.setattr(B, name, getattr(C, name))
    monkeypatch.setattr(C, "RAW", tmp_path / "raw"); monkeypatch.setattr(C, "FEEDBACK", tmp_path / "fb"); monkeypatch.setattr(C, "RUNS", tmp_path / "runs")
    videos, labels = [], []
    for i in range(40):
        vid, ch = f"vid{i}", f"UC{i % 5}"
        videos.append({"video_id": vid, "channel_id": ch, "channel_title": "C", "title": "t", "channel_topics": ["daily_life"],
                       "segments": [{"i": 0, "de": "hallo"}, {"i": 1, "de": "welt"}],
                       "gold": i == 3, "corrections": [{"seg_idx": 0, "kind": "translation", "after": {"lang": "ko", "text": "수정됨"}}] if i == 3 else []})
        labels.append({"video_id": vid, "enrichment": {"segments": [{"i": 0, "de_clean": "Hallo", "ko": "안녕", "en": "hi"}, {"i": 1, "de_clean": "Welt", "ko": "세상", "en": "world"}],
                                                       "glosses": [], "cefr": "A1", "topics": ["daily_life"], "summary_ko": "s"}})
    C.write_jsonl(C.WORK / "transcripts.jsonl", videos)
    C.write_jsonl(C.WORK / "teacher_labels.jsonl", labels)
    C.write_jsonl(C.WORK / "translate_de_ko.jsonl", [{"de": f"satz {i}", "ko": f"문장 {i}", "split": "train"} for i in range(30)])
    out = B.build({"data": {"heldout_channel_fraction": 0.4, "val_fraction": 0.1, "mix": {"translate": 0.4}, "max_examples": {"translate": 10}}})
    rows = {s: C.read_jsonl(C.SFT / f"{s}.jsonl") for s in ("train", "val", "test")}
    ids = {s: {r.get("video_id") for r in rows[s] if r.get("task") == "enrich"} for s in rows}
    assert ids["train"] & ids["test"] == set() and ids["train"] & ids["val"] == set()
    assert "vid3" in ids["test"]                                        # gold video forced into test
    gold_ex = next(r for r in rows["test"] if r.get("video_id") == "vid3")
    assert json.loads(gold_ex["messages"][2]["content"])["segments"][0]["ko"] == "수정됨"   # correction applied
    assert sum(1 for r in rows["train"] if r["task"] == "translate_ko") == 10           # cap respected
    assert out["sizes"]["train"] > 0 and (C.SFT / "stats.json").exists()


def test_metrics():
    assert M.chrf("Hallo Welt", "Hallo Welt") == 100.0
    assert M.chrf("", "x") == 0.0 and M.chrf("abc", "xyz") == 0.0
    assert 30 < M.chrf("Ich trinke Kaffee.", "Ich trinke gern Kaffee.") < 100
    assert M.alignment_accuracy([2, 3, None], [2, 2, 1]) == pytest.approx(1 / 3)
    p, r, f = M.gloss_prf(["der Hund, -e", "trinken"], ["Hund", "essen"])
    assert (p, r) == (0.5, 0.5) and f == pytest.approx(0.5)
    assert M.gloss_prf([], []) == (1.0, 1.0, 1.0)
    assert M.cefr_accuracy(["A1", "B1", None, "C1"], ["A1", "A2", "B1", "A1"]) == (0.25, 0.5)
    assert M.norm_lemma("Die Schwierigkeit, -en") == "schwierigkeit" and M.norm_lemma("an|fangen") == "anfangen"

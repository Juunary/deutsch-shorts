from __future__ import annotations

import json

import httpx
import pytest

from app.db import tx
from pipeline import heuristics as H
from pipeline import transcripts as T
from pipeline import translate as TR
from pipeline.pair import find_pair
from tests.conftest import VIDEO_IDS
from pipeline.youtube_api import (QuotaExceeded, YouTubeAPI, is_dub_description, is_region_blocked,
                                  parse_iso8601_duration, uploads_id, uush_id)


def test_playlist_ids_and_helpers():
    cid = "UC" + "a" * 22
    assert uush_id(cid) == "UUSH" + "a" * 22 and uploads_id(cid) == "UU" + "a" * 22
    with pytest.raises(ValueError):
        uush_id("PL123")
    assert parse_iso8601_duration("PT1M2S") == 62 and parse_iso8601_duration("PT45S") == 45
    assert parse_iso8601_duration("PT1H0M1S") == 3601 and parse_iso8601_duration("PT0S") == 0
    with pytest.raises(ValueError):
        parse_iso8601_duration("nope")
    assert is_region_blocked({"regionRestriction": {"blocked": ["KR"]}})
    assert is_region_blocked({"regionRestriction": {"allowed": ["DE"]}})
    assert not is_region_blocked({"regionRestriction": {"allowed": ["KR", "DE"]}}) and not is_region_blocked({})
    assert is_dub_description("This video is auto-dubbed.") and is_dub_description("Automatisch synchronisiert")
    assert not is_dub_description("Ein normales Video") and not is_dub_description(None)


def test_quota_ledger_hard_stop(db):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"items": []})

    api = YouTubeAPI(api_key="k", conn=db, daily_cap=2, transport=httpx.MockTransport(handler))
    api.videos_details(["a"])
    api.videos_details(["b"])
    assert db.execute("SELECT units FROM quota_ledger").fetchone()[0] == 2 and calls["n"] == 2
    with pytest.raises(QuotaExceeded):
        api.videos_details(["c"])
    assert calls["n"] == 2  # blocked before the request


def test_quota_exceeded_from_api(db):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})

    api = YouTubeAPI(api_key="k", conn=db, transport=httpx.MockTransport(handler))
    with pytest.raises(QuotaExceeded):
        api.resolve_handle("@x")


SNIPPETS = [
    {"text": "Es gibt ja viele Leute, die haben", "start": 0.2, "duration": 3.12},
    {"text": "Schwierigkeiten in Deutschland Freunde", "start": 1.68, "duration": 3.8},
    {"text": "zu finden. Du bist nicht so ein Mensch.", "start": 3.32, "duration": 4.08},
    {"text": "ein &#39;Test&#39;", "start": 8.5, "duration": 1.0},
    {"text": "   ", "start": 8.0, "duration": 1.0},
    {"text": "später", "start": 12.0, "duration": 1.0},
]


def test_merge_segments_rules_and_lookup():
    segs = T.merge_segments(SNIPPETS)
    assert [s["text"] for s in segs] == [
        "Es gibt ja viele Leute, die haben Schwierigkeiten in Deutschland Freunde",  # 12 words = cap
        "zu finden. Du bist nicht so ein Mensch.",                                  # 13 would exceed
        "ein 'Test'",                                                                # html unescaped; gap > 0.7 s before
        "später",
    ]
    assert segs[0]["start_ms"] == 200 and segs[0]["end_ms"] == 3320  # clamped to next start
    assert segs[2]["start_ms"] == 8500 and segs[2]["end_ms"] == 9500 and segs[3]["end_ms"] == 13000
    assert T.lookup(segs, 3320) == 1 and T.lookup(segs, 100) == -1 and T.lookup(segs, 99999) == 3
    # translated snippets merge with the identical grouping
    groups = T.group_snippets(SNIPPETS)
    ko = [{**s, "text": f"k{i}"} for i, s in enumerate(SNIPPETS)]
    assert [s["text"] for s in T.merge_by_groups(ko, groups)] == ["k0 k1", "k2", "k3", "k5"]
    assert T.merge_segments([]) == []


def test_heuristics_levels_and_cefr():
    assert H.tokenize_de("Schöne Grüße, 3 Äpfel!") == ["schöne", "grüße", "äpfel"]
    assert H.word_level("ich") == "A1" and H.word_level("xyzzyq") == "C1"
    assert H.estimate_cefr(0.9, 2.0) == "A1" and H.estimate_cefr(0.9, 3.0) == "A2"
    assert H.estimate_cefr(0.7, 2.0) == "B1" and H.estimate_cefr(0.5, 2.0) == "B2"
    assert H.words_per_second([{"text_de": "eins zwei drei vier", "start_ms": 0, "end_ms": 2000}]) == 2.0
    assert H.words_per_second([]) == 0.0


def test_run_heuristics_never_overwrites_model(seeded_db):
    from app.db import tx
    with tx(seeded_db):
        seeded_db.execute("UPDATE videos SET cefr='B2', cefr_source='model' WHERE id='vid00000002'")
    summary = H.run_heuristics(seeded_db)
    assert summary["computed"] == 2
    rows = {r[0]: (r[1], r[2]) for r in seeded_db.execute("SELECT id, cefr, cefr_source FROM videos")}
    assert rows["vid00000001"][1] == "heuristic" and rows["vid00000002"] == ("B2", "model")
    assert seeded_db.execute("SELECT a1a2_coverage FROM videos WHERE id='vid00000001'").fetchone()[0] > 0.5


def test_translate_providers(monkeypatch):
    def deepl_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["authorization"].startswith("DeepL-Auth-Key") and body["target_lang"] == "KO"
        return httpx.Response(200, json={"translations": [{"text": "안녕"} for _ in body["text"]]})

    monkeypatch.setattr(TR.settings, "deepl_api_key", "abc:fx")
    client = httpx.Client(transport=httpx.MockTransport(deepl_handler))
    assert TR.translate_deepl(["hallo", "welt"], "ko", client=client) == ["안녕", "안녕"]

    seen: list[str] = []

    def google_handler(request: httpx.Request) -> httpx.Response:     # batched POST; first host rate-limited
        seen.append(request.url.host)
        if request.url.host == "translate.googleapis.com":
            return httpx.Response(429, text="<html><title>Sorry...</title></html>")
        assert request.url.params["client"] == "dict-chrome-ex" and request.url.params["sl"] == "de" and request.url.params["tl"] == "ko"
        qs = httpx.QueryParams(request.content.decode())
        return httpx.Response(200, json=[f"T:{q}" for q in qs.get_list("q")])

    monkeypatch.setattr(TR, "GOOGLE_MAX_ITEMS", 2)
    client = httpx.Client(transport=httpx.MockTransport(google_handler))
    assert TR.translate_google(["a", "b", "c"], "ko", client=client, sleep=0) == ["T:a", "T:b", "T:c"]
    assert seen == ["translate.googleapis.com", "clients5.google.com"] * 2          # two chunks, fallback host each time
    assert TR._parse_google([["x", "de"], ["y", "de"]], 2) == ["x", "y"]
    assert TR._parse_google("solo", 1) == ["solo"] and TR._parse_google(["x"], 2) is None
    with pytest.raises(TR.RateLimited):
        TR.translate_google(["a"], "ko", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))))

    monkeypatch.setattr(TR.settings, "mt_provider", "auto")
    assert TR.pick_provider() == "google"                    # a DeepL key no longer changes the default
    monkeypatch.setattr(TR.settings, "mt_provider", "gtx")
    assert TR.pick_provider() == "google"
    monkeypatch.setattr(TR.settings, "mt_provider", "deepl")
    assert TR.pick_provider() == "deepl"
    monkeypatch.setattr(TR.settings, "mt_provider", "none")
    assert TR.pick_provider() is None
    with pytest.raises(TR.TranslateError):
        TR.translate_batch(["x"], "ko")
    monkeypatch.setattr(TR.settings, "mt_provider", "google")
    monkeypatch.setitem(TR.PROVIDERS, "google", lambda texts, tgt, src="de": [x.upper() for x in texts])
    assert TR.translate_batch(["ab"], "en") == ("gtx", ["AB"])         # Google keeps the historical source tag


def test_translate_video_rate_limit_sets_cooldown(seeded_db, monkeypatch):
    import pipeline.transcripts as T
    with tx(seeded_db):
        seeded_db.execute("DELETE FROM translations")

    def limited(texts, lang, provider=None, src="de"):
        raise TR.RateLimited("google: HTTP 429")
    monkeypatch.setattr(T, "translate_batch", limited)
    with pytest.raises(TR.RateLimited):
        T.translate_video(seeded_db, VIDEO_IDS[0])
    assert TR.cooldown_until(seeded_db) is not None
    monkeypatch.setattr(T, "translate_batch", lambda *a, **k: pytest.fail("must not call the provider during cooldown"))
    with pytest.raises(TR.RateLimited):
        T.translate_video(seeded_db, VIDEO_IDS[0])
    s = T.run_translate(seeded_db)
    assert s["videos"] == 0 and s["rate_limited"]
    monkeypatch.setattr(TR.settings, "mt_provider", "none")
    assert T.translate_video(seeded_db, VIDEO_IDS[0]) == {} and T.run_translate(seeded_db)["skipped"]


def test_find_pair_windows():
    de = {"id": "de1", "duration_s": 60, "published_at": "2026-09-01T00:00:00Z"}
    en = [{"id": "far", "duration_s": 60, "published_at": "2026-06-01T00:00:00Z"},
          {"id": "long", "duration_s": 63, "published_at": "2026-09-01T00:00:00Z"},
          {"id": "close", "duration_s": 61, "published_at": "2026-09-05T00:00:00Z"},
          {"id": "closest", "duration_s": 60, "published_at": "2026-09-02T00:00:00Z"}]
    assert find_pair(de, en) == "closest"
    assert find_pair({"id": "x", "duration_s": None}, en) is None
    assert find_pair(de, en[:2]) is None


def test_german_ratio_and_mixed_status(seeded_db):
    from app.db import tx
    de = H.tokenize_de("Ich habe heute keine Zeit und das ist nicht so schlimm, aber wir sehen uns morgen wieder")
    en = H.tokenize_de("What is this called in German? This is the word and you can say it like that, it's easy")
    assert H.german_ratio(de) > 0.8 and H.german_ratio(en) < 0.2 and H.german_ratio(["kurz"]) is None
    with tx(seeded_db):
        seeded_db.execute("UPDATE segments SET text_de=? WHERE video_id='vid00000002' AND idx=0",
                          ("what is this called in german this is the word and you can say it like that and it is easy",))
    s = H.run_heuristics(seeded_db, force=True)
    assert s["mixed"] == 1
    assert seeded_db.execute("SELECT transcript_status FROM videos WHERE id='vid00000002'").fetchone()[0] == "mixed"
    assert seeded_db.execute("SELECT transcript_status FROM videos WHERE id='vid00000001'").fetchone()[0] == "ok"

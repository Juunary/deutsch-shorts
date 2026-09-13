from __future__ import annotations

import json

from app.db import get_setting, tx
from app.models import EventIn
from app.services.feedback import apply_events, decay
from app.services.subtitles import get_subtitles, pick_source
from app.services.takeout import parse_watch_history, topic_weights
from app.services.vocab_export import build_tsv
from tests.conftest import CHANNEL_ID, VIDEO_IDS


def test_pick_source_priority():
    assert pick_source({"gtx": "g", "model": "m", "deepl": "d"}) == ("model", "m")
    assert pick_source({"gtx": "g", "yt_mt": "y"}) == ("yt_mt", "y")
    assert pick_source({}) == (None, None)


def test_get_subtitles(seeded_db):
    out = get_subtitles(seeded_db, VIDEO_IDS[0], "en")
    assert out is not None and out.tr_source == "deepl" and out.segments[1].tr == "[en 1]" and out.glosses == []
    assert get_subtitles(seeded_db, "missing", "ko") is None


def test_feedback_and_decay(seeded_db):
    apply_events(seeded_db, [EventIn(video_id=VIDEO_IDS[0], type="complete"), EventIn(video_id=VIDEO_IDS[0], type="watch", value=0.95),
                             EventIn(video_id=VIDEO_IDS[1], type="embed_error"), EventIn(video_id=VIDEO_IDS[1], type="no_dub")])
    assert abs(seeded_db.execute("SELECT learned FROM topic_affinity WHERE topic='daily_life'").fetchone()[0] - 0.10) < 1e-9
    assert abs(seeded_db.execute("SELECT score FROM channel_affinity WHERE channel_id=?", (CHANNEL_ID,)).fetchone()[0] - 0.10) < 1e-9
    assert tuple(seeded_db.execute("SELECT embeddable, has_dub FROM videos WHERE id=?", (VIDEO_IDS[1],)).fetchone()) == (0, 0)
    for _ in range(20):
        apply_events(seeded_db, [EventIn(video_id=VIDEO_IDS[0], type="complete")])
    assert seeded_db.execute("SELECT learned FROM topic_affinity WHERE topic='daily_life'").fetchone()[0] == 1.0  # clamped
    assert decay(seeded_db) == {"topics": 1, "channels": 1}
    assert abs(seeded_db.execute("SELECT learned FROM topic_affinity WHERE topic='daily_life'").fetchone()[0] - 0.98) < 1e-9
    apply_events(seeded_db, [EventIn(video_id=VIDEO_IDS[0], type="watch", value=0.5)])           # middling watch: no signal
    assert abs(seeded_db.execute("SELECT learned FROM topic_affinity WHERE topic='daily_life'").fetchone()[0] - 0.98) < 1e-9
    assert get_setting(seeded_db, "level_bias") is None


def test_get_subtitles_fills_missing_translations(seeded_db, monkeypatch):
    import pipeline.transcripts as T
    from app.services import subtitles as SUB
    with tx(seeded_db):
        seeded_db.execute("DELETE FROM translations WHERE video_id=? AND lang='ko' AND idx > 0", (VIDEO_IDS[0],))
    calls: list = []

    def fake(texts, lang, provider=None, src="de"):
        calls.append((tuple(texts), lang))
        return "gtx", [f"G:{x}" for x in texts]
    monkeypatch.setattr(T, "translate_batch", fake)
    SUB._fill_backoff.clear()
    out = SUB.get_subtitles(seeded_db, VIDEO_IDS[0], "ko")
    assert [x.tr for x in out.segments] == ["[ko 0]", "G:wie geht es dir", "G:ich trinke kaffee"] and out.tr_source == "gtx"
    assert calls == [(("wie geht es dir", "ich trinke kaffee"), "ko")]                   # only the missing lines, one batch
    assert seeded_db.execute("SELECT count(*) FROM translations WHERE video_id=? AND lang='ko' AND source='gtx'", (VIDEO_IDS[0],)).fetchone()[0] == 2
    assert SUB.get_subtitles(seeded_db, VIDEO_IDS[0], "ko").tr_source == "gtx" and len(calls) == 1   # cached in the DB

    with tx(seeded_db):
        seeded_db.execute("DELETE FROM translations WHERE video_id=? AND lang='en'", (VIDEO_IDS[1],))

    def boom(texts, lang, provider=None, src="de"):
        calls.append("boom")
        raise T.TranslateError("down")
    monkeypatch.setattr(T, "translate_batch", boom)
    out = SUB.get_subtitles(seeded_db, VIDEO_IDS[1], "en")
    assert [x.tr for x in out.segments] == [None, None, None] and out.tr_source is None      # failure is swallowed
    SUB.get_subtitles(seeded_db, VIDEO_IDS[1], "en")
    assert calls.count("boom") == 1                                                           # and not retried at once
    assert SUB.get_subtitles(seeded_db, VIDEO_IDS[1], "en", fill=False).segments[0].tr is None


def test_build_tsv_escapes_and_bolds():
    tsv = build_tsv([{"lemma": "der Hund, -e", "gloss_ko": "개\t강아지", "gloss_en": "dog\nhound", "sentence_de": "Der Hund schläft.",
                      "sentence_ko": "개가 자요.", "surface": "hund", "video_id": "abc", "start_ms": 12500, "handle": "@EasyGerman"}])
    lines = tsv.splitlines()
    assert lines[0] == "#separator:tab" and len(lines) == 4
    cols = lines[3].split("\t")
    assert cols == ["der Hund, -e", "개 강아지 / dog hound", "Der <b>Hund</b> schläft.", "개가 자요.",
                    "https://www.youtube.com/shorts/abc?t=12", "deutsch-shorts EasyGerman"]


def test_takeout_parsing_and_weights():
    data = json.dumps([
        {"header": "YouTube", "title": "Watched Kochen: Brot Rezept", "time": "2026-09-01T00:00:00Z"},
        {"header": "YouTube", "title": "시청한 동영상: 헬스 workout routine", "time": "2026-09-02T00:00:00Z"},
        {"header": "YouTube", "title": "Watched https://www.youtube.com/watch?v=x", "time": "2026-09-02T00:00:00Z"},
        {"header": "Chrome", "title": "Visited something", "time": "2026-09-02T00:00:00Z"},
        {"header": "YouTube", "title": "Watched old Rezept", "time": "2019-01-01T00:00:00Z"},
    ]).encode("utf-8")
    entries = parse_watch_history(data)
    assert [e["title"] for e in entries] == ["헬스 workout routine", "Kochen: Brot Rezept"]
    w = topic_weights(entries)
    assert w["food"] == 1.0 and w["fitness"] == 1.0
    assert parse_watch_history(b"not json") == []

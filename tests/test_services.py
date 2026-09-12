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
    apply_events(seeded_db, [EventIn(video_id=VIDEO_IDS[0], type="like"), EventIn(video_id=VIDEO_IDS[0], type="complete"),
                             EventIn(video_id=VIDEO_IDS[1], type="embed_error"), EventIn(video_id=VIDEO_IDS[1], type="no_dub")])
    assert abs(seeded_db.execute("SELECT learned FROM topic_affinity WHERE topic='daily_life'").fetchone()[0] - 0.20) < 1e-9
    assert abs(seeded_db.execute("SELECT score FROM channel_affinity WHERE channel_id=?", (CHANNEL_ID,)).fetchone()[0] - 0.25) < 1e-9
    assert tuple(seeded_db.execute("SELECT embeddable, has_dub FROM videos WHERE id=?", (VIDEO_IDS[1],)).fetchone()) == (0, 0)
    for _ in range(20):
        apply_events(seeded_db, [EventIn(video_id=VIDEO_IDS[0], type="like")])
    assert seeded_db.execute("SELECT learned FROM topic_affinity WHERE topic='daily_life'").fetchone()[0] == 1.0  # clamped
    assert decay(seeded_db) == {"topics": 1, "channels": 1}
    assert abs(seeded_db.execute("SELECT learned FROM topic_affinity WHERE topic='daily_life'").fetchone()[0] - 0.98) < 1e-9
    apply_events(seeded_db, [EventIn(video_id=VIDEO_IDS[0], type="too_easy")])
    assert get_setting(seeded_db, "level_bias") == {"A2": 0.05}


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

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.db import tx
from app.services import scoring as S
from tests.conftest import VIDEO_IDS

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def test_level_fit_bands():
    ctx = S.Ctx(now=NOW)
    assert S.level_fit("A1", ctx) == 1.0 and S.level_fit("B1", ctx) == 0.3 and S.level_fit("C1", ctx) == 0.0
    ctx.stretch = True
    assert S.level_fit("A1", ctx) == 0.6 and S.level_fit("B1", ctx) == 0.9
    assert S.level_fit(None, ctx) == 0.3


def test_novelty_states():
    ctx = S.Ctx(now=NOW)
    assert S.novelty({}, ctx) == 1.0
    assert S.novelty({"last_impression": NOW - timedelta(hours=1)}, ctx) == 0.1
    assert S.novelty({"last_impression": NOW - timedelta(days=5)}, ctx) == 0.4
    assert S.novelty({"watched_at": NOW - timedelta(days=1), "watched_max": 0.7}, ctx) == 0.0
    assert S.novelty({"completed_at": NOW - timedelta(days=10)}, ctx) == 0.0
    assert S.novelty({"completed_at": NOW - timedelta(days=90)}, ctx) == 1.0


def test_recency_and_topic():
    assert S.recency(NOW, NOW) == 1.0 and 0.36 < S.recency(NOW - timedelta(days=30), NOW) < 0.37 and S.recency(None, NOW) == 0.5
    assert S.topic_score(["food", "tech"], {"food": 1.0}) == 0.5 and S.topic_score([], {"food": 1.0}) == 0.0


def test_score_bonuses():
    base = {"cefr": "A2", "topics": ["food"], "published_dt": NOW, "channel_id": "c", "has_dub": 1, "enrich_status": "ok", "duration_s": 30}
    ctx = S.Ctx(now=NOW, user_topics={"food": 1.0}, prefer_dub=True, llm_on=True)
    s = S.score(base, ctx)
    assert S.score({**base, "has_dub": 0}, ctx) < s
    assert S.score({**base, "enrich_status": "pending"}, ctx) < s
    assert S.score({**base, "duration_s": 120}, ctx) < s


def test_rerank_diverse_penalises_channel_repeats_and_explores():
    scored = [(1.0 - i * 0.001, {"id": f"v{i}", "channel_id": "A" if i < 5 else f"c{i}"}) for i in range(400)]
    picks = S.rerank_diverse(scored, 10, random.Random(1))
    assert picks[0]["id"] == "v0"
    assert picks[1]["channel_id"] != "A"           # second A video (0.999*0.6) loses to c5 (0.995)
    ids = [p["id"] for p in picks]
    assert len(set(ids)) == 10
    # exploration slots 5 and 10 come from ranks 50..300 of the remaining list
    assert int(ids[4][1:]) >= 49 and int(ids[9][1:]) >= 49


def test_build_feed_excludes_recent_impressions(seeded_db):
    items = S.build_feed(seeded_db, n=10)
    assert {i.video_id for i in items} == set(VIDEO_IDS)
    with tx(seeded_db):
        seeded_db.execute("INSERT INTO events(video_id, type, created_at) VALUES(?, 'impression', ?)",
                          (VIDEO_IDS[0], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")))
    # with n=1 the fresh video is enough, so the recently shown one is dropped
    assert [i.video_id for i in S.build_feed(seeded_db, n=1)] == [VIDEO_IDS[1]]
    # with n=10 fewer than n fresh candidates remain -> the 24 h rule is ignored
    assert len(S.build_feed(seeded_db, n=10)) == 2

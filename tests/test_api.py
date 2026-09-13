from __future__ import annotations

import io
import json

from app import config
from app.db import open_db, tx
from tests.conftest import CHANNEL_ID, VIDEO_IDS, seed_data


def _seed_client_db():
    conn = open_db(config.settings.db_file)
    seed_data(conn)
    return conn


def test_auth_required(client):
    assert client.get("/api/health", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/health").status_code == 200


def test_health_and_root(client):
    r = client.get("/api/health")
    body = r.json()
    assert body["ok"] and set(body["counts"]) == {"channels", "videos", "transcripts", "enriched", "vocab"}
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/").status_code == 200
    assert client.get("/manifest.webmanifest").status_code in (200, 404)
    assert client.get("/sw.js").status_code in (200, 404)


def test_feed_and_subtitles(client):
    conn = _seed_client_db()
    items = client.get("/api/feed?n=10").json()["items"]
    assert {i["video_id"] for i in items} == set(VIDEO_IDS)
    assert items[0]["youtube_url"].startswith("https://www.youtube.com/shorts/")
    assert items[0]["channel"]["title"] == "Easy German"
    # exclude works
    rest = client.get(f"/api/feed?n=10&exclude={VIDEO_IDS[0]}").json()["items"]
    assert [i["video_id"] for i in rest] == [VIDEO_IDS[1]]

    subs = client.get(f"/api/videos/{VIDEO_IDS[0]}/subtitles?lang=ko").json()
    assert subs["tr_source"] == "deepl" and [s["tr"] for s in subs["segments"]] == ["[ko 0]", "[ko 1]", "[ko 2]"]
    with tx(conn):
        conn.execute("INSERT INTO translations(video_id, idx, lang, source, text) VALUES(?,?,?,?,?)", (VIDEO_IDS[0], 1, "ko", "model", "모델 번역"))
        conn.execute("INSERT INTO translations(video_id, idx, lang, source, text) VALUES(?,?,?,?,?)", (VIDEO_IDS[0], 2, "ko", "user", "사용자 번역"))
    subs = client.get(f"/api/videos/{VIDEO_IDS[0]}/subtitles?lang=ko").json()
    assert [s["tr"] for s in subs["segments"]] == ["[ko 0]", "모델 번역", "사용자 번역"]
    assert client.get("/api/videos/nope/subtitles").status_code == 404
    assert client.post(f"/api/videos/{VIDEO_IDS[0]}/enrich").json()["status"] == "disabled"


def test_events_feedback_and_corrections(client):
    conn = _seed_client_db()
    r = client.post("/api/events", json=[{"video_id": VIDEO_IDS[0], "type": "complete"}, {"video_id": VIDEO_IDS[0], "type": "watch", "value": 0.1}])
    assert r.json() == {"ok": True, "applied": 2}
    topic = conn.execute("SELECT learned FROM topic_affinity WHERE topic='daily_life'").fetchone()[0]
    chan = conn.execute("SELECT score FROM channel_affinity WHERE channel_id=?", (CHANNEL_ID,)).fetchone()[0]
    assert abs(topic - 0.0) < 1e-9 and abs(chan + 0.05) < 1e-9  # complete (+0.05/+0.05) then skip-equivalent (-0.05/-0.10)
    for gone in ("like", "too_hard", "too_easy"):                # removed features are rejected, not silently stored
        assert client.post("/api/events", json=[{"video_id": VIDEO_IDS[0], "type": gone}]).status_code == 422

    r = client.post("/api/corrections", json={"video_id": VIDEO_IDS[0], "seg_idx": 0, "kind": "translation", "before": {"text": "[ko 0]"}, "after": {"lang": "ko", "text": "좋은 아침"}})
    assert r.json()["ok"] and r.json()["id"] >= 1
    subs = client.get(f"/api/videos/{VIDEO_IDS[0]}/subtitles?lang=ko").json()
    assert subs["segments"][0]["tr"] == "좋은 아침"
    exp = client.get("/api/corrections/export.jsonl?unexported=1&mark=1&token=test-token", headers={"Authorization": ""})
    assert exp.status_code == 200 and json.loads(exp.text.splitlines()[0])["text_de"] == "guten morgen"
    assert client.get("/api/corrections/export.jsonl?unexported=1").text == ""


def test_vocab_crud_and_export(client):
    _seed_client_db()
    body = {"lemma": "trinken", "surface": "trinke", "pos": "verb", "gloss_ko": "마시다", "gloss_en": "to drink",
            "video_id": VIDEO_IDS[0], "seg_idx": 2, "sentence_de": "ich trinke kaffee", "sentence_ko": "저는 커피를 마셔요"}
    a = client.post("/api/vocab", json=body).json()
    b = client.post("/api/vocab", json=body).json()
    assert a["id"] == b["id"]
    assert len(client.get("/api/vocab").json()["items"]) == 1
    tsv = client.get("/api/vocab/export.tsv?mark=1&token=test-token", headers={"Authorization": ""})
    assert tsv.status_code == 200
    lines = tsv.text.splitlines()
    assert lines[:3] == ["#separator:tab", "#html:true", "#tags column:6"]
    cols = lines[3].split("\t")
    assert cols[0] == "trinken" and cols[1] == "마시다 / to drink" and "<b>trinke</b>" in cols[2]
    assert cols[4] == f"https://www.youtube.com/shorts/{VIDEO_IDS[0]}?t=4" and cols[5] == "deutsch-shorts EasyGerman"
    assert client.get("/api/vocab/export.tsv?unexported=1").text.count("\n") == 3
    assert client.delete(f"/api/vocab/{a['id']}").json()["ok"]
    assert client.get("/api/vocab").json()["items"] == []


def test_settings_topics_channels(client):
    _seed_client_db()
    s = client.get("/api/settings").json()
    assert s["mode"] == "listening" and s["topics"] == []
    s = client.put("/api/settings", json={"mode": "reading", "topics": ["food", "science", "bogus"], "onboarded": True}).json()
    assert s["mode"] == "reading" and sorted(s["topics"]) == ["food", "science"] and s["onboarded"] is True
    topics = client.get("/api/topics").json()["items"]
    assert any(t["id"] == "food" and t["label_ko"] for t in topics)
    chans = client.get("/api/channels").json()["items"]
    assert chans[0]["id"] == CHANNEL_ID and chans[0]["video_count"] == 2
    c = client.put(f"/api/channels/{CHANNEL_ID}", json={"enabled": False}).json()
    assert c["enabled"] is False
    assert client.get("/api/feed").json()["items"] == []


def test_takeout_and_admin(client):
    _seed_client_db()
    hist = [{"header": "YouTube", "title": "Watched Bestes Rezept: Brot backen", "time": "2026-09-01T10:00:00Z", "subtitles": [{"name": "Sallys Welt"}]},
            {"header": "YouTube", "title": "Watched Kochen mit Oma", "time": "2026-08-01T10:00:00Z"},
            {"header": "YouTube", "title": "Watched iPhone 20 review", "time": "2020-01-01T10:00:00Z"}]
    r = client.post("/api/import/takeout", files={"file": ("watch-history.json", io.BytesIO(json.dumps(hist).encode()), "application/json")})
    assert r.status_code == 200 and r.json()["titles_used"] == 2 and r.json()["topic_weights"]["food"] == 1.0
    assert client.post("/api/admin/pipeline/run?stage=bogus").status_code == 400
    st = client.get("/api/admin/pipeline/status").json()
    assert st["running"] is False and st["runs"] == []

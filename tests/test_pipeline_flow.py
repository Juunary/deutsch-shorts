from __future__ import annotations

import json

import httpx

from app.db import tx
from app.models import Enrichment, Gloss, Seg
from pipeline import enrich as E
from pipeline import ingest as I
from pipeline import llm_backends as LB
from pipeline import transcripts as T
from app.config import settings
from pipeline.youtube_api import YouTubeAPI
from tests.conftest import CHANNEL_ID, VIDEO_IDS

CID = "UC" + "a" * 22
CP_ID = "UC" + "b" * 22


def _video_item(vid: str, duration: str = "PT45S", embeddable: bool = True, blocked: list[str] | None = None, desc: str = ""):
    cd = {"duration": duration}
    if blocked:
        cd["regionRestriction"] = {"blocked": blocked}
    return {"id": vid, "snippet": {"title": f"T {vid}", "description": desc, "publishedAt": "2026-09-01T00:00:00Z", "defaultAudioLanguage": "de"},
            "contentDetails": cd, "status": {"privacyStatus": "public", "embeddable": embeddable}}


def data_api_handler(request: httpx.Request) -> httpx.Response:
    path, q = request.url.path, dict(request.url.params)
    if path.endswith("/channels"):
        handle = q["forHandle"]
        cid = CID if handle == "EasyGerman" else CP_ID if handle == "kurzgesagt" else None
        return httpx.Response(200, json={"items": [{"id": cid, "snippet": {"title": handle}, "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}}] if cid else []})
    if path.endswith("/playlistItems"):
        pid, token = q["playlistId"], q.get("pageToken")
        if pid.startswith("UUSHbbbb") or pid.startswith("UUSHcccc") or pid.startswith("UUcccc"):
            return httpx.Response(404, json={"error": {"message": "playlist not found", "errors": [{"reason": "playlistNotFound"}]}})
        pages = {None: (["v1", "v2", "v3"], "p2"), "p2": (["v4", "v5"], None)}
        ids, nxt = pages[token]
        body = {"items": [{"contentDetails": {"videoId": v, "videoPublishedAt": "2026-09-01T00:00:00Z"}} for v in ids]}
        if nxt:
            body["nextPageToken"] = nxt
        return httpx.Response(200, json=body)
    if path.endswith("/videos"):
        items = []
        for vid in q["id"].split(","):
            if vid == "v2":
                items.append(_video_item(vid, embeddable=False))
            elif vid == "v3":
                items.append(_video_item(vid, duration="PT5M"))
            elif vid == "v4":
                items.append(_video_item(vid, blocked=[settings.region]))
            elif vid == "v5":
                items.append(_video_item(vid, desc="auto-dubbed by YouTube"))
            else:
                items.append(_video_item(vid))
        return httpx.Response(200, json={"items": items})
    return httpx.Response(404)


def test_seed_and_ingest(db):
    api = YouTubeAPI(api_key="k", conn=db, transport=httpx.MockTransport(data_api_handler))
    entries = [{"handle": "EasyGerman", "id": None, "level_hint": "A2", "topics": ["daily_life"], "dubs": 0, "en_counterpart": "kurzgesagt", "title": None},
               {"handle": "Unknown", "id": None, "level_hint": None, "topics": [], "dubs": 0, "en_counterpart": None, "title": None}]
    s = I.seed(db, api, entries=entries, html_fallback=False)
    assert s["resolved"] == 1 and s["unresolved"] == ["Unknown"] and s["counterparts"] == 1
    ch = {r["id"]: dict(r) for r in db.execute("SELECT * FROM channels")}
    assert ch[CID]["enabled"] == 1 and ch[CID]["shorts_playlist_id"] == "UUSH" + "a" * 22 and ch[CID]["en_counterpart_id"] == CP_ID
    assert ch[CP_ID]["enabled"] == 0
    # re-seeding uses the cached id (no API call) and keeps enabled flags
    with tx(db):
        db.execute("UPDATE channels SET enabled=0 WHERE id=?", (CID,))
    I.seed(db, api, entries=entries[:1], html_fallback=False)
    assert db.execute("SELECT enabled FROM channels WHERE id=?", (CID,)).fetchone()[0] == 0
    with tx(db):
        db.execute("UPDATE channels SET enabled=1 WHERE id=?", (CID,))

    class Checker:
        def is_short(self, vid):
            return vid in ("v1", "v5")

    with tx(db):  # a channel with neither UUSH nor uploads playlist must be skipped, not crash the stage
        db.execute("INSERT INTO channels(id, handle, shorts_playlist_id, level_hint) VALUES(?,?,?,?)", ("UC" + "c" * 22, "Ghost", "UUSH" + "c" * 22, "A1"))
    units_before = api.units_today()
    s = I.ingest(db, api, checker=Checker())
    vids = {r["id"]: dict(r) for r in db.execute("SELECT * FROM videos")}
    # German channel: v1 ok, v2 not embeddable, v3 too long, v4 region-blocked, v5 ok + dub flag
    assert {v for v, r in vids.items() if r["channel_id"] == CID} == {"v1", "v5"}
    assert vids["v5"]["has_dub"] == 1 and vids["v1"]["duration_s"] == 45 and vids["v1"]["default_audio_lang"] == "de"
    # counterpart channel has no UUSH playlist -> uploads + shorts check; the fake ids already exist, so nothing new
    assert s["channels"] == 2 and s["new"] == 2 and s["errors"] == []
    assert db.execute("SELECT last_ingested_at FROM channels WHERE handle='Ghost'").fetchone()[0] is not None
    assert db.execute("SELECT last_ingested_at FROM channels WHERE id=?", (CP_ID,)).fetchone()[0] is not None
    # second run stops at the first known id and inserts nothing
    s2 = I.ingest(db, api)
    assert s2["new"] == 0 and api.units_today() > units_before


def test_ingest_rss_and_backfill(db, monkeypatch):
    with tx(db):
        db.execute("INSERT INTO channels(id, handle, shorts_playlist_id, level_hint) VALUES(?,?,?,?)", (CID, "EasyGerman", "UUSH" + "a" * 22, "A2"))
    monkeypatch.setattr(I, "fetch_playlist_feed", lambda pid, client=None: [{"video_id": "r1", "title": "RSS one", "published_at": "2026-09-10T00:00:00Z"},
                                                                            {"video_id": "v3", "title": "long", "published_at": "2026-09-09T00:00:00Z"}])
    s = I.ingest_rss(db, sleep=0)
    assert s["new"] == 2 and db.execute("SELECT duration_s FROM videos WHERE id='r1'").fetchone()[0] is None
    api = YouTubeAPI(api_key="k", conn=db, transport=httpx.MockTransport(data_api_handler))
    b = I.backfill_details(db, api)
    assert b == {"checked": 2, "updated": 1, "dropped": 1}
    assert db.execute("SELECT duration_s FROM videos WHERE id='r1'").fetchone()[0] == 45
    assert db.execute("SELECT count(*) FROM videos WHERE id='v3'").fetchone()[0] == 0


def test_run_transcripts_with_stub_fetcher(seeded_db, monkeypatch):
    with tx(seeded_db):
        seeded_db.execute("UPDATE videos SET transcript_status='pending'")
        seeded_db.execute("DELETE FROM segments")
        seeded_db.execute("DELETE FROM translations")
    results = {VIDEO_IDS[0]: {"status": "ok", "lang": "de", "is_generated": True,
                              "snippets": [{"text": "Hallo Welt", "start": 0.0, "duration": 1.5}, {"text": "wie geht es", "start": 3.0, "duration": 1.0}]},
               VIDEO_IDS[1]: {"status": "none", "error": "no de"}}
    monkeypatch.setattr(T, "translate_batch", lambda texts, lang, provider=None, src="de": ("deepl", [f"{lang}:{t}" for t in texts]))
    s = T.run_transcripts(seeded_db, limit=10, fetcher=lambda vid: results[vid], sleep_fn=lambda _s: None)
    assert s["ok"] == 1 and s["none"] == 1 and s["translated"] == 2 and not s["blocked"]
    segs = seeded_db.execute("SELECT idx, text_de, start_ms, end_ms FROM segments WHERE video_id=? ORDER BY idx", (VIDEO_IDS[0],)).fetchall()
    assert [tuple(r) for r in segs] == [(0, "Hallo Welt", 0, 1500), (1, "wie geht es", 3000, 4000)]
    assert seeded_db.execute("SELECT text FROM translations WHERE video_id=? AND lang='ko' AND idx=0", (VIDEO_IDS[0],)).fetchone()[0] == "ko:Hallo Welt"
    assert seeded_db.execute("SELECT transcript_status FROM videos WHERE id=?", (VIDEO_IDS[1],)).fetchone()[0] == "none"
    # a block sets a cooldown and aborts; the next run skips
    with tx(seeded_db):
        seeded_db.execute("UPDATE videos SET transcript_status='pending' WHERE id=?", (VIDEO_IDS[1],))
    s = T.run_transcripts(seeded_db, limit=10, fetcher=lambda vid: {"status": "blocked", "error": "IpBlocked"}, sleep_fn=lambda _s: None)
    assert s["blocked"] is True
    assert T.run_transcripts(seeded_db, limit=10, fetcher=lambda vid: results[vid], sleep_fn=lambda _s: None)["skipped_cooldown"] is True
    # generic errors schedule a retry with backoff
    with tx(seeded_db):
        seeded_db.execute("DELETE FROM settings")
    s = T.run_transcripts(seeded_db, limit=10, fetcher=lambda vid: {"status": "error", "error": "boom"}, sleep_fn=lambda _s: None)
    row = seeded_db.execute("SELECT transcript_status, transcript_attempts, next_transcript_try_at FROM videos WHERE id=?", (VIDEO_IDS[1],)).fetchone()
    assert s["failed"] == 1 and row[0] == "failed" and row[1] == 1 and row[2] is not None


class FakeBackend:
    name = "fake"
    model = "fake-1"

    def __init__(self, plan):
        self.plan, self.calls = plan, 0

    def is_ready(self):
        return True

    def enrich(self, payload, system_prompt, error_hint=None):
        step = self.plan[min(self.calls, len(self.plan) - 1)]
        self.calls += 1
        if step == "error":
            raise LB.BackendError("down")
        n = len(payload["segments"])
        if step == "bad":
            return Enrichment(segments=[Seg(i=0, de_clean="x", ko="x", en="x")], cefr="A2"), LB.Usage(1, 1, 0.0, 1, "fake-1")
        segs = [Seg(i=s["i"], de_clean=s["de"].capitalize(), ko=f"한 {s['i']}", en=f"en {s['i']}") for s in payload["segments"]]
        glosses = [Gloss(surface="trinke", lemma="trinken", pos="verb", level="A2", ko="마시다", en="to drink"),
                   Gloss(surface="Zebra", lemma="das Zebra", pos="noun", level="B1", ko="얼룩말", en="zebra")]
        return Enrichment(segments=segs, glosses=glosses, cefr="A1", topics=["food", "nope"], summary_ko="요약"), LB.Usage(100, 50, 0.001, 20, "fake-1")


def test_enrich_one_paths(seeded_db):
    vid = VIDEO_IDS[0]
    ok = FakeBackend(["bad", "good"])
    assert E.enrich_one(seeded_db, vid, backend=ok) == "ok" and ok.calls == 2
    assert seeded_db.execute("SELECT count(*) FROM translations WHERE video_id=? AND source='model'", (vid,)).fetchone()[0] == 6
    assert [r[0] for r in seeded_db.execute("SELECT surface FROM glosses WHERE video_id=?", (vid,))] == ["trinke"]
    assert seeded_db.execute("SELECT seg_idx FROM glosses WHERE video_id=?", (vid,)).fetchone()[0] == 2
    v = seeded_db.execute("SELECT cefr, cefr_source, topics_json, summary_ko, enrich_status FROM videos WHERE id=?", (vid,)).fetchone()
    assert tuple(v) == ("A1", "model", '["food"]', "요약", "ok")
    e = seeded_db.execute("SELECT status, input_tokens, output_tokens FROM enrichments WHERE video_id=?", (vid,)).fetchone()
    assert tuple(e) == ("ok", 101, 51)
    assert E.enrich_one(seeded_db, vid, backend=ok) == "skipped"          # cached
    assert E.enrich_one(seeded_db, vid, backend=ok, force=True) == "ok"
    assert E.enrich_one(seeded_db, VIDEO_IDS[1], backend=FakeBackend(["bad", "bad"])) == "fallback"
    assert E.enrich_one(seeded_db, VIDEO_IDS[1], backend=FakeBackend(["error"])) == "failed"
    assert seeded_db.execute("SELECT enrich_status FROM videos WHERE id=?", (VIDEO_IDS[1],)).fetchone()[0] == "failed"
    assert E.enrich_one(seeded_db, "missing", backend=ok) == "skipped"
    s = E.run_enrich(seeded_db, backend=FakeBackend(["good"]), retry_failed=True, limit=5)
    assert s["ok"] == 1 and s["llm_calls"] == 1


def test_local_backend_openai_and_ollama():
    good = Enrichment(segments=[Seg(i=0, de_clean="Hallo", ko="안녕", en="hi")], cefr="A1", topics=["daily_life"], summary_ko="s").model_dump()

    def openai_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(404)
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["schema"]["properties"]["cefr"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(good) + "\n```"}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 34}})

    b = LB.LocalBackend(url="http://llm", model="m", transport=httpx.MockTransport(openai_handler))
    enr, usage = b.enrich({"segments": [{"i": 0, "de": "hallo"}]}, "sys")
    assert b.kind() == "openai" and enr.segments[0].ko == "안녕" and (usage.input_tokens, usage.output_tokens) == (12, 34)

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        body = json.loads(request.content)
        assert body["format"]["type"] == "object" and body["stream"] is False
        return httpx.Response(200, json={"message": {"content": json.dumps(good)}, "prompt_eval_count": 5, "eval_count": 6})

    b2 = LB.LocalBackend(url="http://ollama", model="m", transport=httpx.MockTransport(ollama_handler))
    assert b2.kind() == "ollama" and b2.is_ready()
    enr2, usage2 = b2.enrich({"segments": [{"i": 0, "de": "hallo"}]}, "sys")
    assert enr2.cefr == "A1" and usage2.output_tokens == 6

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}) if request.url.path != "/api/tags" else httpx.Response(404)

    import pytest
    with pytest.raises(LB.BackendError):
        LB.LocalBackend(url="http://x", transport=httpx.MockTransport(broken)).enrich({"segments": []}, "sys")
    assert LB.price("claude-opus-5", 1_000_000, 0) == 5.0 and LB.price("unknown", 10, 10) == 0.0

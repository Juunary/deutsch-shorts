from __future__ import annotations

import json

from app.db import open_db, tx
from pipeline.sync import export_content, import_content
from tests.conftest import CHANNEL_ID, VIDEO_IDS


def test_export_import_roundtrip_keeps_user_state(seeded_db, tmp_path):
    with tx(seeded_db):
        seeded_db.execute("INSERT INTO glosses(video_id, surface_lc, surface, lemma, pos, level, gloss_ko) VALUES(?,?,?,?,?,?,?)",
                          (VIDEO_IDS[0], "kaffee", "kaffee", "der Kaffee", "noun", "A2", "커피"))
        seeded_db.execute("INSERT INTO glosses(video_id, surface_lc, surface, lemma, pos, level, gloss_ko) VALUES(?,?,?,?,?,?,?)",
                          (VIDEO_IDS[0], "morgen", "morgen", "der Morgen", "noun", "A2", "아침"))
        seeded_db.execute("INSERT INTO enrichments(video_id, backend, model, prompt_version, status, raw_json) VALUES(?,?,?,?,?,?)",
                          (VIDEO_IDS[0], "local", "teacher", "enrich_v1", "ok", "{}"))
        seeded_db.execute("UPDATE videos SET enrich_status='ok', cefr='A1', cefr_source='model' WHERE id=?", (VIDEO_IDS[0],))
    path = tmp_path / "content.jsonl"
    counts = export_content(seeded_db, path)
    assert counts["channels"] == 1 and counts["videos"] == 2 and counts["segments"] == 6 and counts["glosses"] == 2

    # target DB: user already edited things that must survive the import
    target = open_db(tmp_path / "target.db")
    with tx(target):
        target.execute("INSERT INTO channels(id, handle, shorts_playlist_id, enabled) VALUES(?,?,?,0)", (CHANNEL_ID, "EasyGerman", "UUSHtest000000000000000000"))
        target.execute("INSERT INTO videos(id, channel_id, embeddable, transcript_status, cefr, cefr_source) VALUES(?,?,0,'pending','B1','user')", (VIDEO_IDS[0], CHANNEL_ID))
        target.execute("INSERT INTO segments(video_id, idx, start_ms, end_ms, text_de) VALUES(?,?,?,?,?)", (VIDEO_IDS[0], 0, 0, 1000, "old"))
        target.execute("INSERT INTO segments(video_id, idx, start_ms, end_ms, text_de) VALUES(?,?,?,?,?)", (VIDEO_IDS[0], 9, 0, 1000, "stale"))
        target.execute("INSERT INTO translations(video_id, idx, lang, source, text) VALUES(?,?,?,?,?)", (VIDEO_IDS[0], 0, "ko", "user", "내 번역"))
        target.execute("INSERT INTO corrections(video_id, seg_idx, kind, after_json) VALUES(?,?,?,?)",
                       (VIDEO_IDS[0], 0, "gloss", json.dumps({"surface": "Kaffee", "wrong": True})))
    c = import_content(target, path)
    assert c["videos"] == 2 and c["segments"] == 6
    v = target.execute("SELECT enabled FROM channels WHERE id=?", (CHANNEL_ID,)).fetchone()[0]
    assert v == 0                                                                   # channel toggle kept
    row = target.execute("SELECT embeddable, transcript_status, enrich_status, cefr, cefr_source FROM videos WHERE id=?", (VIDEO_IDS[0],)).fetchone()
    assert tuple(row) == (0, "ok", "ok", "B1", "user")                             # embed error + user CEFR kept, statuses upgraded
    assert target.execute("SELECT count(*) FROM segments WHERE video_id=?", (VIDEO_IDS[0],)).fetchone()[0] == 3   # stale rows replaced
    assert target.execute("SELECT text FROM translations WHERE video_id=? AND idx=0 AND source='user'", (VIDEO_IDS[0],)).fetchone()[0] == "내 번역"
    assert target.execute("SELECT count(*) FROM translations WHERE video_id=? AND source='deepl'", (VIDEO_IDS[0],)).fetchone()[0] == 6
    assert [r[0] for r in target.execute("SELECT surface_lc FROM glosses WHERE video_id=?", (VIDEO_IDS[0],))] == ["morgen"]   # removed gloss stays removed
    assert target.execute("SELECT count(*) FROM enrichments").fetchone()[0] == 1
    # idempotent
    c2 = import_content(target, path)
    assert c2 == c and target.execute("SELECT count(*) FROM segments").fetchone()[0] == 6

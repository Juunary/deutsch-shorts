-- deutsch-shorts initial schema (SQLite). Timestamps are ISO-8601 UTC strings.

CREATE TABLE IF NOT EXISTS channels (
  id                 TEXT PRIMARY KEY,               -- UC...
  handle             TEXT,                           -- @handle (without @ also accepted)
  title              TEXT,
  shorts_playlist_id TEXT,                           -- UUSH... (UC prefix replaced by UUSH)
  level_hint         TEXT,                           -- A1 | A2 | B1 | B2 | C1
  topics_json        TEXT NOT NULL DEFAULT '[]',
  dubs               INTEGER NOT NULL DEFAULT 0,     -- 1 = channel known to publish dubbed/multi-audio
  en_counterpart_id  TEXT,                           -- channel id publishing the same content in English
  enabled            INTEGER NOT NULL DEFAULT 1,
  last_ingested_at   TEXT,
  created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS videos (
  id                     TEXT PRIMARY KEY,
  channel_id             TEXT NOT NULL REFERENCES channels(id),
  title                  TEXT,
  description            TEXT,
  published_at           TEXT,
  duration_s             INTEGER,
  default_audio_lang     TEXT,
  embeddable             INTEGER NOT NULL DEFAULT 1,
  region_blocked         INTEGER NOT NULL DEFAULT 0,
  has_dub                INTEGER NOT NULL DEFAULT 0, -- 0 none/unknown, 1 description heuristic, 2 channel-known
  pair_video_id          TEXT,
  transcript_status      TEXT NOT NULL DEFAULT 'pending', -- pending | ok | none | disabled | failed
  transcript_attempts    INTEGER NOT NULL DEFAULT 0,
  next_transcript_try_at TEXT,
  enrich_status          TEXT NOT NULL DEFAULT 'pending', -- pending | ok | fallback | failed | skipped
  cefr                   TEXT,
  cefr_source            TEXT,                        -- model | heuristic | channel
  a1a2_coverage          REAL,
  wps                    REAL,
  topics_json            TEXT NOT NULL DEFAULT '[]',
  summary_ko             TEXT,
  first_seen_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_transcript_status ON videos(transcript_status);
CREATE INDEX IF NOT EXISTS idx_videos_enrich_status ON videos(enrich_status);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at);

CREATE TABLE IF NOT EXISTS transcript_raw (
  video_id     TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
  lang         TEXT NOT NULL,
  is_generated INTEGER NOT NULL DEFAULT 1,
  json         TEXT NOT NULL,                        -- raw snippets [{text,start,duration}]
  fetched_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS segments (
  video_id      TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  idx           INTEGER NOT NULL,
  start_ms      INTEGER NOT NULL,
  end_ms        INTEGER NOT NULL,
  text_de       TEXT NOT NULL,
  text_de_clean TEXT,
  PRIMARY KEY (video_id, idx)
);

CREATE TABLE IF NOT EXISTS translations (
  video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  idx      INTEGER NOT NULL,
  lang     TEXT NOT NULL,                             -- ko | en
  source   TEXT NOT NULL,                             -- model | yt_mt | user
  text     TEXT NOT NULL,
  PRIMARY KEY (video_id, idx, lang, source)
);
CREATE INDEX IF NOT EXISTS idx_translations_video_lang ON translations(video_id, lang);

CREATE TABLE IF NOT EXISTS glosses (
  video_id   TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  surface_lc TEXT NOT NULL,
  surface    TEXT NOT NULL,
  lemma      TEXT NOT NULL,
  pos        TEXT NOT NULL,
  level      TEXT,
  gloss_ko   TEXT,
  gloss_en   TEXT,
  seg_idx    INTEGER,
  PRIMARY KEY (video_id, surface_lc)
);

CREATE TABLE IF NOT EXISTS enrichments (
  video_id       TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
  backend        TEXT NOT NULL,                       -- claude | local
  model          TEXT,
  prompt_version TEXT,
  status         TEXT NOT NULL,                       -- ok | fallback | failed
  raw_json       TEXT,
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  cost_usd       REAL,
  latency_ms     INTEGER,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS corrections (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id    TEXT NOT NULL,
  seg_idx     INTEGER,
  kind        TEXT NOT NULL,                          -- translation | gloss | level
  before_json TEXT,
  after_json  TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  exported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_corrections_exported ON corrections(exported_at);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id   TEXT NOT NULL,
  type       TEXT NOT NULL,   -- impression|play|watch|complete|like|skip|save_word|embed_error|too_hard|too_easy|no_dub
  value      REAL,
  mode       TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_events_video ON events(video_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS vocab (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  lemma       TEXT NOT NULL,
  surface     TEXT,
  pos         TEXT,
  gloss_ko    TEXT,
  gloss_en    TEXT,
  video_id    TEXT,
  seg_idx     INTEGER,
  sentence_de TEXT,
  sentence_ko TEXT,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  exported_at TEXT,
  UNIQUE (lemma, video_id)
);

CREATE TABLE IF NOT EXISTS topic_affinity (
  topic      TEXT PRIMARY KEY,
  base       REAL NOT NULL DEFAULT 0,
  learned    REAL NOT NULL DEFAULT 0,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS channel_affinity (
  channel_id TEXT PRIMARY KEY,
  score      REAL NOT NULL DEFAULT 0,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  stage        TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  quota_units  INTEGER NOT NULL DEFAULT 0,
  ok           INTEGER NOT NULL DEFAULT 0,
  failed       INTEGER NOT NULL DEFAULT 0,
  llm_calls    INTEGER NOT NULL DEFAULT 0,
  llm_cost_usd REAL NOT NULL DEFAULT 0,
  notes        TEXT
);

CREATE TABLE IF NOT EXISTS quota_ledger (
  day          TEXT PRIMARY KEY,
  units        INTEGER NOT NULL DEFAULT 0,
  search_calls INTEGER NOT NULL DEFAULT 0
);

-- Grok Bot quota tracker schema (v1). Stores counts, sizes, ids, names,
-- timestamps and statuses only. Never message text, prompts, arguments,
-- results, URLs, emails or tokens.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                 -- bot | child | unknown
  display_name TEXT,
  slug TEXT,
  first_seen_ms INTEGER,
  last_seen_ms INTEGER,
  profile_present INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,         -- path relative to the data root
  source_type TEXT NOT NULL,          -- store | audit | transcript | automation | runs |
                                      -- search_index_messages | search_index_media |
                                      -- cli_claude | cli_codex | cli_grok (rows live in cli_runs, not events)
                                      -- cli_launches (rows live in cli_launches, not events)
  entity_id TEXT,
  first_seen_ms INTEGER,
  last_seen_ms INTEGER,
  status TEXT                         -- ok | missing | error
);

CREATE TABLE IF NOT EXISTS cursors (
  source_id TEXT PRIMARY KEY,
  generation INTEGER NOT NULL DEFAULT 0,
  device INTEGER,
  inode INTEGER,
  byte_offset INTEGER NOT NULL DEFAULT 0,
  last_seq INTEGER NOT NULL DEFAULT 0,
  boundary_hash TEXT,
  mtime_ns INTEGER,
  size_bytes INTEGER,
  last_success_ms INTEGER,
  error_code TEXT,
  backlog_bytes INTEGER NOT NULL DEFAULT 0
);

-- Append-only event ledger. One row per normalized observation.
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  source_key TEXT NOT NULL,           -- seq / entry id / byte offset / run id+hash
  event_index INTEGER NOT NULL DEFAULT 0,
  event_type TEXT NOT NULL,
  occurred_at_ms INTEGER,             -- exact source timestamp, when present
  time_basis TEXT NOT NULL,           -- exact | window | interpolated | none
  win_lo_ms INTEGER,                  -- live window for undated transcript lines
  win_hi_ms INTEGER,
  win_frac REAL,                      -- byte position of the line within the file
  entity_id TEXT,                     -- execution owner
  peer_id TEXT,                       -- other agent (message sender/recipient, child)
  request_id TEXT,
  turn_id TEXT,
  run_id TEXT,
  call_id TEXT,
  tool_name TEXT,
  tool_category TEXT,
  mcp_server TEXT,
  status TEXT,
  duration_ms INTEGER,
  model_label TEXT,
  effort TEXT,                        -- always NULL in v1: no source exposes it
  text_chars INTEGER NOT NULL DEFAULT 0,
  text_bytes INTEGER NOT NULL DEFAULT 0,
  argument_chars INTEGER NOT NULL DEFAULT 0,
  argument_bytes INTEGER NOT NULL DEFAULT 0,
  result_chars INTEGER NOT NULL DEFAULT 0,
  result_bytes INTEGER NOT NULL DEFAULT 0,
  attachment_bytes INTEGER NOT NULL DEFAULT 0,
  actions INTEGER NOT NULL DEFAULT 0,
  screenshots INTEGER NOT NULL DEFAULT 0,
  safe_metadata_json TEXT,
  -- projections rebuilt by attribute.py
  est_at_ms INTEGER,
  est_day TEXT,                       -- Europe/Lisbon date of est_at_ms
  root_entity_id TEXT,                -- causal owner (top of parent chain)
  attr_run_id TEXT,                   -- routine run this event belongs to
  attr_routine_id TEXT,
  attr_confidence TEXT,
  UNIQUE (source_id, generation, source_key, event_index)
);
CREATE INDEX IF NOT EXISTS ix_events_entity_time ON events(entity_id, est_at_ms);
CREATE INDEX IF NOT EXISTS ix_events_root_day ON events(root_entity_id, est_day);
CREATE INDEX IF NOT EXISTS ix_events_request ON events(request_id);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(attr_run_id);
CREATE INDEX IF NOT EXISTS ix_events_call ON events(call_id);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS ix_events_source ON events(source_id, generation);
CREATE INDEX IF NOT EXISTS ix_events_entity_key ON events(entity_id, source_key);

CREATE TABLE IF NOT EXISTS links (
  link_id INTEGER PRIMARY KEY,
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  relation TEXT NOT NULL,             -- parent-child | message | request-run
  evidence_event_id INTEGER,
  confidence TEXT NOT NULL,           -- high | medium | low
  method TEXT NOT NULL,
  created_at_ms INTEGER,
  UNIQUE (from_id, to_id, relation, method)
);
CREATE INDEX IF NOT EXISTS ix_links_to ON links(to_id, relation);

CREATE TABLE IF NOT EXISTS routines (
  owner_id TEXT NOT NULL,
  routine_id TEXT NOT NULL,           -- automation folder name
  name TEXT,
  enabled INTEGER,
  trigger_type TEXT,
  schedule TEXT,
  timezone TEXT,
  prompt_chars INTEGER,
  prompt_sha256 TEXT,
  config_hash TEXT,
  PRIMARY KEY (owner_id, routine_id)
);

CREATE TABLE IF NOT EXISTS runs (
  owner_id TEXT NOT NULL,
  routine_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  request_id TEXT,
  trigger TEXT,
  started_at_ms INTEGER,
  finished_at_ms INTEGER,
  status TEXT,
  record_hash TEXT,
  latest_event_id INTEGER,
  PRIMARY KEY (owner_id, routine_id, run_id)
);

CREATE TABLE IF NOT EXISTS collector_runs (
  collector_run_id INTEGER PRIMARY KEY,
  started_at_ms INTEGER,
  ended_at_ms INTEGER,
  script_version TEXT,
  mode TEXT,
  runtime_ms INTEGER,
  bytes_read INTEGER,
  rows_inspected INTEGER,
  events_inserted INTEGER,
  backlog_bytes INTEGER,
  backlog_sources INTEGER,
  errors INTEGER,
  error_summary TEXT,
  db_bytes INTEGER,
  report_bytes INTEGER,
  tracker_request_id TEXT,
  tracker_run_id TEXT
);

-- Time span of each entity's detailed transcript (rebuilt by attribute.py).
-- Search-index events inside the span are already counted by the transcript.
CREATE TABLE IF NOT EXISTS detail_spans (
  entity_id TEXT PRIMARY KEY,
  lo_ms INTEGER NOT NULL,
  hi_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_snapshots (
  snapshot_id INTEGER PRIMARY KEY,
  taken_at_ms INTEGER,
  percentage REAL,
  remaining TEXT,
  resets_at_ms INTEGER,
  source TEXT,
  window_identity TEXT,
  confidence TEXT
);

-- CLI sessions. Not an event source: nothing here is joined into
-- v_canonical_activity, v_scored_activity or the score.
-- cost_usd stays NULL when the tool does not report a price.
-- Codex estimates live in cost_usd_est (cost_estimated=1) and never replace cost_usd.
-- model_defaulted=1 when that estimate priced an unknown model as default_model.
CREATE TABLE IF NOT EXISTS cli_runs (
  source_id TEXT NOT NULL,
  run_key TEXT PRIMARY KEY,
  tool TEXT NOT NULL CHECK (tool IN ('claude', 'codex', 'grok')),
  task TEXT,
  bot_id TEXT NOT NULL DEFAULT 'unattributed',
  session_id TEXT,
  ts TEXT,                            -- ISO UTC
  model TEXT,
  num_turns INTEGER,
  duration_ms INTEGER,
  is_error INTEGER,
  cost_usd REAL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_create_tokens INTEGER,
  reasoning_tokens INTEGER,
  subagent_count INTEGER,
  raw_json TEXT,
  cwd TEXT,                           -- working directory, for launch matching
  attribution_method TEXT,            -- launch_session | launch_cwd | plugin | unattributed
  cost_usd_est REAL,
  cost_estimated INTEGER,             -- 1 when cost_usd_est is the figure to show
  model_defaulted INTEGER             -- 1 when the estimate used prices.default_model
);
CREATE INDEX IF NOT EXISTS ix_cli_runs_tool ON cli_runs(tool, ts);
CREATE INDEX IF NOT EXISTS ix_cli_runs_task ON cli_runs(task);
CREATE INDEX IF NOT EXISTS ix_cli_runs_bot ON cli_runs(bot_id);
CREATE INDEX IF NOT EXISTS ix_cli_runs_method ON cli_runs(attribution_method);

-- One row per grokbot-run launch line. Not an event source and not scored.
-- Prompts and tool arguments are never stored.
CREATE TABLE IF NOT EXISTS cli_launches (
  launch_key TEXT PRIMARY KEY,        -- "<source_id>:<byte offset>"
  source_id TEXT NOT NULL,
  bot_id TEXT,
  task TEXT,
  tool TEXT NOT NULL CHECK (tool IN ('claude', 'codex', 'grok')),
  cwd TEXT,
  started_at TEXT,                    -- ISO 8601 as logged, with UTC offset
  pid INTEGER,
  session_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_cli_launches_session ON cli_launches(tool, session_id);
CREATE INDEX IF NOT EXISTS ix_cli_launches_started ON cli_launches(tool, started_at);

-- Events from the current generation of each source only (a rewritten or
-- replaced file bumps the generation and is re-read from the start).
-- Search-index rows (source_key "<agent>/<entry>") that a current store.db
-- event already holds (same agent, entry id) are dropped as duplicates.
DROP VIEW IF EXISTS v_canonical_activity;
CREATE VIEW v_canonical_activity AS
SELECT e.* FROM events e
JOIN cursors c ON c.source_id = e.source_id AND c.generation = e.generation
WHERE e.source_id NOT LIKE 'search-index.db#%' OR NOT EXISTS (
  SELECT 1 FROM events s JOIN cursors sc ON sc.source_id = s.source_id AND sc.generation = s.generation
  WHERE s.entity_id = e.entity_id AND s.source_key = substr(e.source_key, instr(e.source_key, '/') + 1)
    AND s.source_id LIKE 'agents/%/store.db');

-- Activity that feeds the score: search-index events only count outside the
-- entity's detailed transcript span (inside it, inference steps and text are
-- already counted from the transcript).
DROP VIEW IF EXISTS v_scored_activity;
CREATE VIEW v_scored_activity AS
SELECT a.* FROM v_canonical_activity a
WHERE a.source_id NOT LIKE 'search-index.db#%' OR NOT EXISTS (
  SELECT 1 FROM detail_spans d WHERE d.entity_id = a.entity_id AND a.est_at_ms BETWEEN d.lo_ms AND d.hi_ms);

DROP VIEW IF EXISTS v_attributed_activity;
CREATE VIEW v_attributed_activity AS
SELECT event_id, event_type, est_day, est_at_ms, time_basis,
       entity_id AS exec_owner,
       COALESCE(root_entity_id, entity_id, 'unknown') AS causal_owner,
       attr_routine_id, attr_run_id, attr_confidence
FROM v_canonical_activity;

-- Score formula "score-v2" (see README). Not a quota percentage.
-- proxy_replies: search-index assistant messages outside transcript coverage,
-- a weaker (lower-bound) stand-in for inference steps.
DROP VIEW IF EXISTS v_daily_entity;
CREATE VIEW v_daily_entity AS
WITH base AS (
  SELECT 'direct' AS scope, COALESCE(entity_id, 'unknown') AS entity_id, * FROM v_scored_activity
  UNION ALL
  SELECT 'inclusive', COALESCE(root_entity_id, entity_id, 'unknown'), * FROM v_scored_activity
)
SELECT scope, entity_id, COALESCE(est_day, 'undated') AS day,
  SUM(event_type = 'request_started') AS root_requests,
  SUM(event_type = 'inference_step') AS inference_steps,
  SUM(event_type = 'tool_called') AS tool_calls,
  SUM(event_type = 'tool_called' AND tool_category = 'subagent') AS child_launches,
  SUM(actions) AS cu_actions,
  SUM(screenshots) AS screenshots,
  SUM(event_type IN ('message', 'message_sent')) AS messages,
  SUM(text_bytes + argument_bytes + result_bytes) AS bytes,
  SUM(text_chars + argument_chars + result_chars) AS chars,
  SUM(source_id LIKE 'search-index.db#%' AND event_type = 'message' AND status = 'assistant') AS proxy_replies,
  SUM(source_id LIKE 'search-index.db#%') AS search_index_events,
  ROUND(
      1.0 * SUM(event_type = 'inference_step')
    + 0.5 * SUM(event_type = 'tool_called')
    + 5.0 * SUM(event_type = 'tool_called' AND tool_category = 'subagent')
    + 0.2 * SUM(actions)
    + 1.0 * SUM(screenshots)
    + 1.0 * SUM(event_type = 'request_started')
    + 1.0 * SUM(source_id LIKE 'search-index.db#%' AND event_type = 'message' AND status = 'assistant')
    + SUM(text_bytes + argument_bytes + result_bytes) / 4000.0, 1) AS score
FROM base
GROUP BY scope, entity_id, day;

DROP VIEW IF EXISTS v_routine_runs;
CREATE VIEW v_routine_runs AS
SELECT r.owner_id, r.routine_id, r.run_id, r.request_id, r.trigger, r.status,
       r.started_at_ms, r.finished_at_ms,
       r.finished_at_ms - r.started_at_ms AS duration_ms,
       COUNT(a.event_id) AS linked_events,
       COALESCE(SUM(a.event_type = 'inference_step'), 0) AS inference_steps,
       COALESCE(SUM(a.event_type = 'tool_called'), 0) AS tool_calls,
       COALESCE(SUM(a.event_type LIKE 'audit_%'), 0) AS audit_events,
       COALESCE(SUM(a.event_type = 'tool_called' AND a.tool_category = 'subagent'), 0) AS child_launches,
       MAX(a.attr_confidence) AS attribution_confidence
FROM runs r
LEFT JOIN v_canonical_activity a ON a.attr_run_id = r.run_id
GROUP BY r.owner_id, r.routine_id, r.run_id;

DROP VIEW IF EXISTS v_coverage;
CREATE VIEW v_coverage AS
SELECT s.source_type,
       COUNT(*) AS sources,
       SUM(s.status = 'ok') AS ok,
       SUM(s.status = 'missing') AS missing,
       SUM(s.status = 'error') AS errored,
       SUM(COALESCE(c.size_bytes, 0)) AS size_bytes,
       SUM(COALESCE(c.backlog_bytes, 0)) AS backlog_bytes,
       SUM(c.backlog_bytes > 0) AS sources_with_backlog,
       MAX(c.last_success_ms) AS last_success_ms
FROM sources s LEFT JOIN cursors c ON c.source_id = s.source_id
GROUP BY s.source_type;

-- Hand-entered plan usage percentages; never scored as activity.
CREATE TABLE IF NOT EXISTS usage_readings (
  day TEXT PRIMARY KEY,
  usage_pct REAL,
  note TEXT,
  recorded_at_ms INTEGER
);

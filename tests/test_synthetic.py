import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "examples", "synthetic"))

import build  # noqa: E402
from quota import collect  # noqa: E402
from quota import dashboard, report  # noqa: E402
from datetime import date

ALPHA = "00000000-0000-4000-8000-000000000001"
BETA = "00000000-0000-4000-8000-000000000002"
SI_MESSAGES = "search-index.db#messages"
CHILD = "sand-subagent-00000000-0000-4000-8000-00000000000a"
TRANSCRIPT = f"agent-transcripts/{ALPHA}/{ALPHA}.jsonl"


class SyntheticCollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = build.build(os.path.join(self.tmp.name, "agent-data"))
        self.db_path = os.path.join(self.tmp.name, "quota", "quota.db")
        self.panel = os.path.join(self.tmp.name, "quota", "panel.html")

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, *extra):
        # Keep the suite off the real ~/.codex, ~/.grok and launches trees.
        out = io.StringIO()
        isolated = ["--codex-sessions", os.path.join(self.tmp.name, "codex-sessions"),
                    "--grok-sessions", os.path.join(self.tmp.name, "grok-sessions"),
                    "--launches", os.path.join(self.tmp.name, "launches.jsonl")]
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            collect.main(["--db", self.db_path, "--root", self.data, "--panel", self.panel, *isolated, *extra])
        return out.getvalue()

    def db(self):
        con = sqlite3.connect(self.db_path)
        self.addCleanup(con.close)
        return con

    def one(self, sql, *args):
        return self.db().execute(sql, args).fetchone()[0]

    def test_backfill_report_and_privacy(self):
        out = self.collect("--backfill", "--report")
        self.assertIn("SUMMARY", out)
        self.assertIn("Alpha Bot", out)
        # identities: two bots with profiles, one child
        self.assertEqual(self.one("SELECT COUNT(*) FROM entities WHERE kind='bot' AND profile_present=1"), 2)
        self.assertEqual(self.one("SELECT kind FROM entities WHERE entity_id=?", CHILD), "child")
        # store, audit, transcript and routine events
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE event_type='request_started'"), 2)
        self.assertEqual(self.one("SELECT SUM(screenshots) FROM events WHERE event_type='computer_session'"), 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE event_type='inference_step'"), 5)
        # wrapped built-in Task via CallMcpTool is unwrapped into a child launch
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE tool_category='subagent' AND "
                                  "event_type='tool_called'"), 2)
        self.assertEqual(self.one("SELECT mcp_server FROM events WHERE tool_name='CallMcpTool'"), "user-Gmail")
        self.assertEqual(self.one("SELECT attachment_bytes FROM events WHERE event_type='attachment'"), 2048)
        # partial trailing line stays pending
        self.assertGreater(self.one("SELECT backlog_bytes FROM cursors WHERE source_id=?", TRANSCRIPT), 0)
        # child link + causal owner
        self.assertEqual(self.one("SELECT confidence FROM links WHERE from_id=? AND to_id=? AND method='task_result'",
                                  ALPHA, CHILD), "high")
        self.assertEqual(self.one("SELECT DISTINCT root_entity_id FROM events WHERE entity_id=?", CHILD), ALPHA)
        self.assertIsNotNone(self.one("SELECT MIN(est_at_ms) FROM events WHERE entity_id=?", CHILD))
        # message sender link
        self.assertEqual(self.one("SELECT COUNT(*) FROM links WHERE relation='message'"), 1)
        # routine run attribution via requestId/turnId
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE attr_run_id='run-1' AND attr_confidence='high'"
                                  " AND event_type LIKE 'audit_%'"), 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM runs"), 2)
        self.assertEqual(self.one("SELECT prompt_chars FROM routines"), len("CANARY-ROUTINE-PROMPT do the thing"))
        # self-metering
        self.assertGreaterEqual(self.one("SELECT COUNT(*) FROM collector_runs"), 1)
        self.assertGreater(self.one("SELECT report_bytes FROM collector_runs ORDER BY collector_run_id DESC"), 0)
        self.assertEqual(self.one("PRAGMA journal_mode"), "wal")
        # flags fire on the synthetic routine (2x/day schedule, a failed run)
        with open(self.panel, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("frequent_automation", html)
        self.assertIn("failed_routine_runs", html)
        self.assertNotIn("<script src", html)
        # privacy: no content, prompts, args, URLs, paths or secrets anywhere
        self.db().execute("PRAGMA wal_checkpoint(FULL)")
        blobs = [html]
        for p in (self.db_path, self.db_path + "-wal"):
            if os.path.exists(p):
                with open(p, "rb") as f:
                    blobs.append(f.read().decode("utf-8", "replace"))
        for blob in blobs:
            self.assertNotIn("CANARY", blob)
            self.assertNotIn("example.com", blob)

    def test_dashboard_build_and_privacy(self):
        self.collect('--backfill', '--report')
        with open(self.panel, 'rb') as f:
            panel_before = f.read()
        out = os.path.join(self.tmp.name, 'dashboard.html')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(dashboard.main(['--db', self.db_path, '--out', out,
                                            '--start', '2026-09-20', '--end', '2026-09-30']), 0)
        with open(out, encoding='utf-8') as f:
            page = f.read()
        for label in ("This week's spend", 'Last 30 days', 'Routines', 'Computer use',
                      'Alpha Bot', '2 runs, 1 failed', '<svg', 'lang="en"'):
            self.assertIn(label, page)
        for marker in ('CANARY', 'example.com', '<script', '<link', 'https://'):
            self.assertNotIn(marker, page)
        self.assertIn('class="chart"', page)
        self.assertEqual(page.count('no tool data'), 0)
        with open(self.panel, 'rb') as f:
            self.assertEqual(f.read(), panel_before)
        empty = dashboard.build_html(self.db(), date(2099, 1, 1), date(2099, 1, 7))
        self.assertIn('No activity recorded in this period.', empty)
        self.assertEqual(dashboard.schedule_text('21 8,15 * * 1-5'),
                         '2x a day on weekdays')

    def test_incremental_idempotent_append_and_rewrite(self):
        self.collect("--backfill")
        n0 = self.one("SELECT COUNT(*) FROM events")
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM events"), n0, "replay must not duplicate")
        self.assertEqual(self.one("SELECT events_inserted FROM collector_runs ORDER BY collector_run_id DESC"), 0)

        # complete the partial line -> appended record, timed by the live window
        path = os.path.join(self.data, TRANSCRIPT)
        with open(path, "a") as f:
            f.write("\n")
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE time_basis='window' AND event_type='inference_step'"), 1)
        self.assertEqual(self.one("SELECT backlog_bytes FROM cursors WHERE source_id=?", TRANSCRIPT), 0)

        # rewrite (truncate + different content) -> new generation, canonical view not doubled
        with open(path, "w") as f:
            f.write(json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}) + "\n")
        self.collect()
        self.assertEqual(self.one("SELECT generation FROM cursors WHERE source_id=?", TRANSCRIPT), 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM v_canonical_activity WHERE source_id=? AND "
                                  "event_type='inference_step'", TRANSCRIPT), 1)

        # run state change -> new routine_run_state event, projection updated
        runs = os.path.join(self.data, "agents", ALPHA, "automations", "daily-check", "runs.json")
        with open(runs) as f:
            data = json.load(f)
        data.insert(0, {"id": "run-2", "trigger": "schedule", "startedAt": 1790090000000, "finishedAt": None,
                        "status": "running"})
        with open(runs, "w") as f:
            json.dump(data, f)
        self.collect()
        data[0].update(finishedAt=1790090100000, status="ok")
        with open(runs, "w") as f:
            json.dump(data, f)
        os.utime(runs, ns=(1, 2))  # make sure mtime differs even on coarse clocks
        self.collect()
        self.assertEqual(self.one("SELECT status FROM runs WHERE run_id='run-2'"), "ok")
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE run_id='run-2'"), 2)

    def test_search_index_dedupe_coverage_and_window(self):
        out = self.collect("--backfill", "--report", "--start", "2026-09-20", "--end", "2026-09-30")
        self.assertIn("for 2026-09-20..2026-09-30:", out)
        self.assertIn("search-index replies", out)
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE source_id=?", SI_MESSAGES), 6)
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE source_id='search-index.db#media'"), 2)
        # entries already in store.db (message e2, send-message e3, attachment e6) are not counted twice
        canon = {r[0] for r in self.db().execute(
            "SELECT source_key FROM v_canonical_activity WHERE source_id LIKE 'search-index.db#%'")}
        self.assertEqual(canon, {f"{ALPHA}/t1a0", f"{ALPHA}/t2u", f"{ALPHA}/t2a0", f"{BETA}/t2a0", f"{BETA}/t3m"})
        # a reply inside Alpha's transcript span is already counted as inference steps: not scored
        scored = {r[0] for r in self.db().execute(
            "SELECT source_key FROM v_scored_activity WHERE source_id LIKE 'search-index.db#%'")}
        self.assertEqual(scored, canon - {f"{ALPHA}/t1a0"})
        # sizes computed in SQL (chars vs UTF-8 bytes), role kept, exact timestamp
        self.assertEqual(self.db().execute("SELECT status, text_chars, text_bytes, time_basis FROM events "
                                           "WHERE source_key=?", (f"{ALPHA}/t2a0",)).fetchone(),
                         ("assistant", len("CANARY-SI-REPLY yes"), len("CANARY-SI-REPLY yes".encode()), "exact"))
        # media: kind/ext/size only; timestamp 0 is undated
        self.assertEqual(self.db().execute("SELECT status, time_basis, safe_metadata_json FROM events "
                                           "WHERE source_key=?", (f"{BETA}/t3m",)).fetchone(),
                         ("pdf", "none", '{"ext": ".pdf", "kind": "pdf"}'))
        # search-index-only day: one proxy reply plus text bytes
        row = self.db().execute("SELECT proxy_replies, inference_steps, bytes, score FROM v_daily_entity "
                                "WHERE scope='direct' AND entity_id=? AND day='2026-09-28'", (ALPHA,)).fetchone()
        self.assertEqual(row[:3], (1, 0, len("CANARY-SI-USER hello".encode()) + len("CANARY-SI-REPLY yes".encode())))
        self.assertEqual(row[3], round(1 + row[2] / 4000, 1))
        # the mirror does not widen transcript interpolation bounds
        self.assertLess(self.one("SELECT MAX(est_at_ms) FROM events WHERE source_id=?", TRANSCRIPT), 1790600000000)
        with open(self.panel, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("search_index_messages", html)
        self.assertIn("Top spenders in report window: 2026-09-20..2026-09-30", html)

        # default window (last 7 days ending today) is empty for this fixture: falls back
        self.assertIn("(fallback: nothing dated in the last 7 days)", self.collect("--report"))

    def test_search_index_incremental(self):
        self.collect("--backfill")
        n0 = self.one("SELECT COUNT(*) FROM events")
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM events"), n0, "replay must not duplicate")
        self.assertEqual(self.one("SELECT last_seq FROM cursors WHERE source_id=?", SI_MESSAGES), 22)
        si = sqlite3.connect(os.path.join(self.data, "search-index.db"))
        self.addCleanup(si.close)
        ins = "INSERT INTO messages (id, agent_id, entry_id, role, timestamp_ms, body) VALUES (?,?,?,?,?,?)"
        with si:
            si.execute(ins, (30, BETA, "t4a0", "assistant", 1790700000000, "CANARY-NEW"))
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM events"), n0 + 1)
        # watermark row replaced by a different entry under the same id: rescan, no duplicates
        with si:
            si.execute("DELETE FROM messages WHERE id=30")
            si.execute(ins, (30, BETA, "t5a0", "assistant", 1790800000000, "CANARY-REPLACED"))
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM events"), n0 + 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE source_key=?", f"{BETA}/t5a0"), 1)

    def test_budget_limits_pass(self):
        self.collect("--max-read-mib", "0.0001")
        self.assertGreater(self.one("SELECT backlog_bytes FROM collector_runs"), 0)
        self.collect("--backfill")
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE event_type='inference_step'"), 5)


if __name__ == "__main__":
    unittest.main()

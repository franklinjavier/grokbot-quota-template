"""CLI session collector: claude jsonl, codex rollouts, grok usage snapshots."""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from zoneinfo import ZoneInfo
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quota import collect, dashboard  # noqa: E402
from quota.plugins.sqlite_tasks import TaskIndex  # noqa: E402
from quota.rows import bind as bind_rows  # noqa: E402

ALPHA = "00000000-0000-4000-8000-000000000001"
BETA = "00000000-0000-4000-8000-000000000002"
SIDE = "00000000-0000-4000-8000-000000000003"
OTHER = "00000000-0000-4000-8000-000000000009"
NORTH = "00000000-0000-4000-8000-000000000004"
EXAMPLES = os.path.join(ROOT, "examples", "cli")


class CliRunsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "agent-data")
        self.codex = os.path.join(self.tmp.name, "codex")
        self.grok = os.path.join(self.tmp.name, "grok")
        self.db_path = os.path.join(self.tmp.name, "quota.db")
        self.panel = os.path.join(self.tmp.name, "panel.html")
        self.dash = os.path.join(self.tmp.name, "dashboard.html")
        os.makedirs(self.root)
        report_dir = os.path.join(self.root, "reports", "JOB-124")
        os.makedirs(report_dir)
        with open(os.path.join(report_dir, "claude-1.jsonl"), "w", encoding="utf-8") as f:
            f.write(
                '{"type": "user", "timestamp": "2026-10-01T12:00:00.000Z"}\n'
                '{"type": "assistant", "timestamp": "2026-10-01T12:00:05.000Z"}\n'
                '{"type": "result", "is_error": false, "duration_ms": 1500, "num_turns": 2, '
                '"session_id": "sess-claude-1", "total_cost_usd": 0.25, '
                '"usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 3, '
                '"cache_creation_input_tokens": 4, "output_tokens_details": {"thinking_tokens": 5}}, '
                '"modelUsage": {"claude-opus": {}, "claude-haiku": {}}, '
                '"subagent_stats": [{"id": "s1"}, {"id": "s2"}]}\n'
            )
        shutil.copytree(os.path.join(EXAMPLES, "codex"), self.codex)
        shutil.copytree(os.path.join(EXAMPLES, "grok"), self.grok)
        # Second glob (reports/*/*/claude-*.jsonl) plus an unattributed task.
        nested = os.path.join(self.root, "reports", "group", "not-a-task")
        os.makedirs(nested)
        with open(os.path.join(nested, "claude-n.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"type":"system","timestamp":"2026-10-04T00:00:00Z"}\n')
            f.write(json.dumps({
                "type": "result", "is_error": False, "duration_ms": 5, "num_turns": 1,
                "session_id": "sess-nested", "total_cost_usd": 1.0,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "modelUsage": {"m": {}},
                "subagent_stats": {"spawned": 4, "failed": 1},
            }) + "\n")
        for eid, name in ((ALPHA, "Alpha Bot"), (BETA, "Beta Bot")):
            folder = os.path.join(self.root, "agents", eid)
            os.makedirs(folder)
            with open(os.path.join(folder, "profile.json"), "w", encoding="utf-8") as f:
                json.dump({"name": name}, f)
        self.tasks = os.path.join(self.root, "tasks.db")
        con = sqlite3.connect(self.tasks)
        con.execute("CREATE TABLE projects (id TEXT, name TEXT, bot_id TEXT)")
        con.execute("CREATE TABLE tasks (id TEXT, project_id TEXT)")
        con.executemany("INSERT INTO projects VALUES (?,?,?)", [
            ("project-alpha", "Project Alpha", ALPHA),
            ("project-demo", "Project Demo", BETA),
            ("project", "Project", OTHER),  # shorter needle; must lose to project-demo
            ("project-side", "Project Side", SIDE),
            ("north-demo", "Demo North", NORTH),
            ("skills", "Skills", None),
        ])
        con.execute("INSERT INTO tasks VALUES ('JOB-124', 'project-alpha')")
        con.commit()
        con.close()
        with open(self.tasks, "rb") as f:
            self.tasks_hash = hashlib.sha256(f.read()).hexdigest()
        self._saved_env = {key: os.environ.get(key) for key in (
            "QUOTA_ATTRIBUTION_PLUGIN", "QUOTA_TASKS_DB", "QUOTA_REPORTS_GLOB")}
        os.environ["QUOTA_ATTRIBUTION_PLUGIN"] = "quota.plugins.sqlite_tasks:lookup"
        os.environ["QUOTA_TASKS_DB"] = self.tasks
        os.environ.pop("QUOTA_REPORTS_GLOB", None)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def collect(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            collect.main(["--db", self.db_path, "--root", self.root, "--panel", self.panel,
                          "--codex-sessions", self.codex, "--grok-sessions", self.grok,
                          "--launches", os.path.join(self.tmp.name, "launches.jsonl"),
                          "--backfill", *extra])
        return out.getvalue()

    def db(self):
        con = sqlite3.connect(self.db_path)
        bind_rows(con)
        self.addCleanup(con.close)
        return con

    def one(self, sql, *args):
        return self.db().execute(sql, args).fetchone()[0]

    def row(self, sql, *args):
        return self.db().execute(sql, args).fetchone()

    def test_parsing_attribution_and_score_isolation(self):
        out = self.collect("--report")
        with open(self.tasks, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), self.tasks_hash)
        self.assertFalse(os.path.exists(self.tasks + "-wal"))

        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs"), 4)
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs WHERE tool='claude'"), 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs WHERE tool='codex'"), 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs WHERE tool='grok'"), 1)
        # CLI rows are not events and do not move the score.
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM events WHERE source_id LIKE 'reports/%' "
            "OR source_id LIKE 'codex-sessions/%' OR source_id LIKE 'grok-sessions/%'"), 0)
        self.assertNotIn("cli_runs", self.one("SELECT sql FROM sqlite_master WHERE name='v_scored_activity'"))
        self.assertEqual(self.one("SELECT COALESCE(SUM(score),0) FROM v_daily_entity"), 0)

        claude = self.row("SELECT * FROM cli_runs WHERE session_id='sess-claude-1'")
        self.assertTrue(claude["run_key"].startswith("claude:reports/JOB-124/claude-1.jsonl:"))
        self.assertEqual(claude["task"], "JOB-124")
        self.assertEqual(claude["bot_id"], ALPHA)  # tasks.id → project bot, not a cwd guess
        self.assertEqual(claude["attribution_method"], "plugin")
        self.assertIsNone(claude["cwd"])
        self.assertEqual(claude["ts"], "2026-10-01T12:00:05.000000Z")
        self.assertEqual(claude["model"], "claude-opus,claude-haiku")
        self.assertEqual(claude["num_turns"], 2)
        self.assertEqual(claude["duration_ms"], 1500)
        self.assertEqual(claude["is_error"], 0)
        self.assertEqual(claude["cost_usd"], 0.25)
        self.assertEqual((claude["input_tokens"], claude["output_tokens"], claude["cache_read_tokens"],
                          claude["cache_create_tokens"], claude["reasoning_tokens"], claude["subagent_count"]),
                         (10, 20, 3, 4, 5, 2))
        raw = json.loads(claude["raw_json"])
        self.assertEqual(raw["usage"]["input_tokens"], 10)
        self.assertNotIn("message", raw)

        nested = self.row("SELECT * FROM cli_runs WHERE session_id='sess-nested'")
        self.assertEqual(nested["task"], "not-a-task")
        self.assertEqual(nested["bot_id"], "unattributed")
        self.assertEqual(nested["attribution_method"], "unattributed")
        self.assertEqual(nested["subagent_count"], 4)  # dict summary uses spawned, not len(keys)
        self.assertEqual(nested["ts"], "2026-10-04T00:00:00.000000Z")
        self.assertEqual(nested["source_id"], "reports/group/not-a-task/claude-n.jsonl")

        codex = self.row("SELECT * FROM cli_runs WHERE tool='codex'")
        self.assertEqual(codex["run_key"], "codex:codex-sess-1")
        self.assertEqual(codex["session_id"], "codex-sess-1")
        self.assertEqual(codex["task"], "JOB-195")
        self.assertEqual(codex["bot_id"], BETA)  # longest project id in the cwd, not "project"
        self.assertEqual(codex["cwd"], "/work/project-demo-job-195")
        self.assertEqual(codex["attribution_method"], "plugin")
        self.assertIsNone(codex["cost_usd"])
        self.assertEqual(codex["model"], "gpt-6.1-sol")  # turn_context wins over session_meta
        self.assertEqual(codex["cost_estimated"], 1)
        self.assertEqual(codex["model_defaulted"], 0)
        self.assertAlmostEqual(codex["cost_usd_est"], 457 / 1_000_000)
        self.assertEqual(codex["ts"], "2026-10-02T00:02:00.000000Z")
        self.assertEqual((codex["input_tokens"], codex["cache_read_tokens"], codex["cache_create_tokens"],
                          codex["output_tokens"], codex["reasoning_tokens"]),
                         (10, 20, 30, 40, 50))
        self.assertEqual(codex["source_id"], "codex-sessions/2026/10/02/rollout-1.jsonl")

        grok = self.row("SELECT * FROM cli_runs WHERE tool='grok'")
        self.assertEqual(grok["run_key"], "grok:grok-sess-1")
        self.assertEqual(grok["task"], "project-side")
        self.assertEqual(grok["bot_id"], SIDE)
        self.assertEqual(grok["cwd"], "/work/project-side")
        self.assertEqual(grok["attribution_method"], "plugin")
        self.assertEqual(grok["model"], "grok-4")
        self.assertEqual(grok["num_turns"], 3)
        self.assertEqual(grok["ts"], "2026-10-03T08:00:00.000000Z")
        self.assertAlmostEqual(grok["cost_usd"], 2.5)
        self.assertEqual((grok["input_tokens"], grok["output_tokens"], grok["cache_read_tokens"],
                          grok["cache_create_tokens"], grok["reasoning_tokens"]),
                         (100, 50, 7, 8, 9))

        # Paths stay out of the scrubbed metrics blob. cwd is stored on its own column.
        raws = " ".join((r[0] or "") for r in self.db().execute("SELECT raw_json FROM cli_runs"))
        self.assertNotIn("/work/project-demo-job-195", raws)
        self.assertNotIn("/work/project-side", raws)

        idx = TaskIndex(self.tasks)
        self.assertEqual(idx.bot_for("JOB-124", "/work/project-demo-job-195"), ALPHA)
        self.assertEqual(idx.bot_for("misc", "/tmp/Demo North/repo"), NORTH)
        self.assertEqual(idx.bot_for("nope", "/tmp/zzz"), "unattributed")
        self.assertEqual(collect.task_from_cwd("/work/bot-alpha-task167"), "bot-alpha-task167")
        self.assertEqual(collect.subagent_count([1, 2]), 2)
        self.assertEqual(collect.subagent_count({"a": 1}), 1)
        self.assertEqual(collect.subagent_count(None), 0)

        self.assertIn("CLI runs (not included in the score):", out)
        self.assertIn("claude: 2 runs, cost_usd 1.25, tokens 44", out)
        self.assertIn("codex: 1 run, cost_usd 0.00 (estimated), tokens 150", out)
        self.assertIn("budgets: ok", out)
        self.assertNotIn("EXCESSIVE:", out)
        self.assertIn("grok: 1 run, cost_usd 2.50, tokens 174", out)
        with open(self.panel, encoding="utf-8") as f:
            panel = f.read()
        self.assertIn("CLI runs", panel)
        self.assertIn("Not included in the score", panel)
        self.assertIn("attribution: launch_session 0, launch_cwd 0, plugin 3, unattributed 1", panel)

    def test_incremental_cursor(self):
        self.collect()
        n0 = self.one("SELECT COUNT(*) FROM cli_runs")
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs"), n0)
        self.assertEqual(self.one("SELECT backlog_bytes FROM cursors WHERE source_id=?",
                                  "reports/JOB-124/claude-1.jsonl"), 0)
        codex_in = self.one("SELECT input_tokens FROM cli_runs WHERE run_key='codex:codex-sess-1'")

        claude_path = os.path.join(self.root, "reports", "JOB-124", "claude-1.jsonl")
        extra = {"type": "result", "is_error": True, "duration_ms": 10, "num_turns": 1,
                 "session_id": "sess-claude-2", "total_cost_usd": 0.5,
                 "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0, "output_tokens_details": {"thinking_tokens": 0}},
                 "modelUsage": {"claude-haiku": {}},
                 "subagent_stats": {"spawned": 1, "failed": 0}}
        line = json.dumps(extra)
        with open(claude_path, "a", encoding="utf-8") as f:
            f.write(line)  # no newline yet: must stay pending
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs"), n0)
        self.assertGreater(self.one("SELECT backlog_bytes FROM cursors WHERE source_id=?",
                                    "reports/JOB-124/claude-1.jsonl"), 0)
        with open(claude_path, "a", encoding="utf-8") as f:
            f.write("\n")
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs"), n0 + 1)
        added = self.row("SELECT * FROM cli_runs WHERE session_id='sess-claude-2'")
        self.assertEqual(added["is_error"], 1)
        self.assertEqual(added["subagent_count"], 1)
        self.assertEqual(added["bot_id"], ALPHA)
        # No new timestamp on the appended line: keep the last one seen earlier.
        self.assertEqual(added["ts"], "2026-10-01T12:00:05.000000Z")

        rollout = os.path.join(self.codex, "2026", "10", "02", "rollout-1.jsonl")
        with open(rollout, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": "2026-10-02T00:03:00Z", "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 99, "cached_input_tokens": 1, "cache_write_input_tokens": 1,
                    "output_tokens": 1, "reasoning_output_tokens": 1}}},
            }) + "\n")
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs WHERE tool='codex'"), 1)
        self.assertEqual(self.one("SELECT input_tokens FROM cli_runs WHERE run_key='codex:codex-sess-1'"), 99)
        self.assertNotEqual(codex_in, 99)
        self.assertEqual(self.one("SELECT ts FROM cli_runs WHERE run_key='codex:codex-sess-1'"),
                         "2026-10-02T00:03:00.000000Z")
        self.assertEqual(self.one("SELECT model FROM cli_runs WHERE run_key='codex:codex-sess-1'"),
                         "gpt-6.1-sol")

        usage = os.path.join(self.grok, "demo", "grok-sess-1", "usage.json")
        with open(usage, encoding="utf-8") as f:
            data = json.loads(f.read())
        data["session"]["inputTokens"] = 111111
        data["updatedAt"] = "2026-10-03T09:00:00Z"
        with open(usage, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.utime(usage, ns=(1, 2))
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs WHERE tool='grok'"), 1)
        self.assertEqual(self.one("SELECT input_tokens FROM cli_runs WHERE run_key='grok:grok-sess-1'"), 111111)
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs WHERE run_key='grok:grok-sess-1'"), 1)

    def test_dashboard_panel(self):
        self.collect()
        # Fixture CLI rows are 1–4 Oct. Anchor the week there so this week's tables include them.
        moment = datetime(2026, 10, 4, 15, tzinfo=ZoneInfo("Europe/Lisbon"))
        page = dashboard.build_html(
            self.db(), date(2026, 9, 20), date(2026, 9, 30), now=moment)
        self.assertIn("attribution: launch_session 0, launch_cwd 0, plugin 3, unattributed 1", page)
        self.assertNotIn("no tool data", page)
        self.assertEqual(page.count("no tool data"), 0)
        for label in ("Alpha Bot", "Beta Bot", "Unattributed", "JOB-124", "JOB-195", "project-side",
                      "not-a-task", "$0.25", "$2.50", "adopt grokbot-run"):
            self.assertIn(label, page)
        self.assertNotIn(OTHER, page)
        self.assertNotIn("Most active", page)
        self.assertNotIn("Day by day", page)
        # The activity chart ends 30 Sep, before these CLI rows, so it stays empty.
        self.assertIn("No activity recorded in this period.", page)
        self.assertLess(page.index("<h2>Bots</h2>"), page.index("This week's spend"))
        self.assertLess(page.index("<h2>Bots</h2>"), page.index("<summary>Tasks</summary>"))

    def test_codex_model_reread_does_not_double_count(self):
        self.collect()
        codex = self.row("SELECT * FROM cli_runs WHERE tool='codex'")
        sid = codex["source_id"]
        tokens = codex["input_tokens"]
        gen = self.one("SELECT generation FROM cursors WHERE source_id=?", sid)
        self.assertEqual(self.one("SELECT backlog_bytes FROM cursors WHERE source_id=?", sid), 0)
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE cli_runs SET model=NULL, cost_usd_est=NULL, cost_estimated=NULL, "
            "model_defaulted=NULL WHERE run_key=?", (codex["run_key"],))
        con.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id) "
            "VALUES (?, 'codex:sentinel', 'codex', 'unattributed')", (sid,))
        key = collect.CLI_STATE + sid
        state = json.loads(con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0])
        state.pop("model", None)
        con.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(state, sort_keys=True), key))
        con.execute("DELETE FROM meta WHERE key=?", (collect.CODEX_MODEL_EPOCH_KEY,))
        con.commit()
        con.close()
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs WHERE run_key=?", codex["run_key"]), 1)
        self.assertEqual(self.one("SELECT input_tokens FROM cli_runs WHERE run_key=?", codex["run_key"]), tokens)
        self.assertEqual(self.one("SELECT model FROM cli_runs WHERE run_key=?", codex["run_key"]), "gpt-6.1-sol")
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs WHERE run_key='codex:sentinel'"), 1)
        self.assertEqual(self.one("SELECT generation FROM cursors WHERE source_id=?", sid), gen)
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs WHERE tool='codex'"), 2)

    def test_schema_migration_is_idempotent(self):
        bare = os.path.join(self.tmp.name, "old.db")
        con = sqlite3.connect(bare)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.commit()
        con.close()
        con = collect.open_db(bare)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='cli_runs'").fetchone()[0], 1)
        con.close()
        con = collect.open_db(bare)
        con.execute("SELECT COUNT(*) FROM cli_runs")
        con.close()


if __name__ == "__main__":
    unittest.main()

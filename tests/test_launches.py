"""grokbot-run launch log and collector attribution."""
import errno
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quota import collect  # noqa: E402
from quota.rows import bind as bind_rows  # noqa: E402

ALPHA = "00000000-0000-4000-8000-000000000001"
BETA = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000009"
LAUNCHER = os.path.join(ROOT, "bin", "grokbot-run")


class GrokbotRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")
        self.work = os.path.join(self.tmp.name, "work")
        os.makedirs(self.work)
        self.fake = os.path.join(self.tmp.name, "fake-tool.py")
        with open(self.fake, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "json.dump({'bot': os.environ.get('GROKBOT_BOT_ID'),"
                " 'task': os.environ.get('GROKBOT_TASK'),"
                " 'pid': os.getpid(), 'args': sys.argv[1:]}, sys.stdout)\n"
                "sys.stdout.write('\\n')\n"
                "raise SystemExit(7)\n"
            )
        os.chmod(self.fake, 0o755)
        self.launches = os.path.join(self.state, "launches.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args):
        env = os.environ.copy()
        env["GROKBOT_QUOTA_STATE"] = self.state
        env["GROKBOT_RUN_TOOL_CLAUDE"] = self.fake
        env["GROKBOT_RUN_TOOL_CODEX"] = self.fake
        env["GROKBOT_RUN_TOOL_GROK"] = self.fake
        return subprocess.run([LAUNCHER, *args], env=env, cwd=self.work,
                              capture_output=True, text=True)

    def lines(self):
        with open(self.launches, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_exec_preserves_status_env_and_session(self):
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        proc = self.run_tool("bot-1", "JOB-9", "--", "claude", "--print", "CANARY-PROMPT")
        self.assertEqual(proc.returncode, 7, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["bot"], out["task"]), ("bot-1", "JOB-9"))
        self.assertEqual(out["args"][0], "--session-id")
        sid = out["args"][1]
        self.assertRegex(sid, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertEqual(out["args"][2:], ["--print", "CANARY-PROMPT"])
        line = self.lines()[0]
        self.assertEqual(set(line), {"bot_id", "task", "tool", "cwd", "started_at", "pid", "session_id"})
        self.assertEqual(line["bot_id"], "bot-1")
        self.assertEqual(line["task"], "JOB-9")
        self.assertEqual(line["tool"], "claude")
        self.assertEqual(line["cwd"], self.work)
        self.assertEqual(line["pid"], out["pid"])
        self.assertIsInstance(line["pid"], int)
        self.assertEqual(line["session_id"], sid)
        self.assertRegex(line["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        off = datetime.now().astimezone().strftime("%z")
        self.assertTrue(line["started_at"].endswith(off[:3] + ":" + off[3:]))
        with open(self.launches, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("CANARY-PROMPT", raw)
        self.assertEqual(raw.count("\n"), 1)

        proc = self.run_tool("bot-1", "JOB-9", "--", "claude", "--session-id", "fixed-sess", "--yes")
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(json.loads(proc.stdout)["args"], ["--session-id", "fixed-sess", "--yes"])
        self.assertEqual(self.lines()[1]["session_id"], "fixed-sess")

        proc = self.run_tool("bot-1", "JOB-9", "--", "claude", "--resume", "old-sess")
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(json.loads(proc.stdout)["args"], ["--resume", "old-sess"])
        self.assertIsNone(self.lines()[2]["session_id"])

        proc = self.run_tool("bot-1", "JOB-9", "--", "claude", "-r", "old-sess")
        self.assertNotIn("--session-id", json.loads(proc.stdout)["args"])
        self.assertIsNone(self.lines()[3]["session_id"])

        proc = self.run_tool("bot-1", "JOB-9", "--", "claude", "-c")
        self.assertEqual(json.loads(proc.stdout)["args"], ["-c"])
        self.assertIsNone(self.lines()[4]["session_id"])

        proc = self.run_tool("bot-2", "job-2", "--", "codex", "exec")
        self.assertEqual(proc.returncode, 7)
        body = json.loads(proc.stdout)
        self.assertEqual((body["bot"], body["task"], body["args"]), ("bot-2", "job-2", ["exec"]))
        self.assertIsNone(self.lines()[5]["session_id"])
        self.assertEqual(self.lines()[5]["tool"], "codex")
        self.assertEqual(self.lines()[5]["pid"], body["pid"])

        proc = self.run_tool("bot-2", "job-2", "--", "codex", "resume", "codex-sess")
        self.assertEqual(json.loads(proc.stdout)["args"], ["resume", "codex-sess"])
        self.assertEqual(self.lines()[6]["session_id"], "codex-sess")

        proc = self.run_tool("bot-3", "job-3", "--", "grok", "--session-id=grok-sess", "hi")
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(json.loads(proc.stdout)["args"], ["--session-id=grok-sess", "hi"])
        self.assertEqual(self.lines()[7]["session_id"], "grok-sess")
        self.assertIsNone(self.lines()[7].get("args"))

    def test_misuse_exits_2(self):
        proc = self.run_tool("bot-1", "JOB-9", "--", "claude", "-p", "x")
        self.assertEqual(proc.returncode, 7)
        with open(self.launches, encoding="utf-8") as f:
            before = f.read()
        cases = [
            [],
            ["only-bot"],
            ["bot", "task", "claude"],
            ["bot", "task", "--"],
            ["bot", "task", "--", "python"],
            ["", "task", "--", "claude"],
            ["bot", "", "--", "claude"],
        ]
        for args in cases:
            proc = self.run_tool(*args)
            self.assertEqual(proc.returncode, 2, args)
            self.assertIn("usage:", proc.stderr)
            self.assertEqual(proc.stdout, "")
        with open(self.launches, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)


def write_claude(root, task, session, ts):
    folder = os.path.join(root, "reports", task)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "claude-1.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "timestamp": ts}) + "\n")
        f.write(json.dumps({
            "type": "result", "is_error": False, "duration_ms": 5, "num_turns": 1,
            "session_id": session, "total_cost_usd": 0.1,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {"m": {}},
        }) + "\n")


def write_codex(codex, name, session, cwd, ts):
    folder = os.path.join(codex, "2026", "10", "02")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"rollout-{name}.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-10-02T00:00:00Z", "type": "session_meta",
            "payload": {"session_id": session, "cwd": cwd},
        }) + "\n")
        f.write(json.dumps({
            "timestamp": ts, "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 1, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                "output_tokens": 1, "reasoning_output_tokens": 0}}},
        }) + "\n")


def write_tasks(root, projects, tasks=()):
    con = sqlite3.connect(os.path.join(root, "tasks.db"))
    con.execute("CREATE TABLE projects (id TEXT, name TEXT, bot_id TEXT)")
    con.execute("CREATE TABLE tasks (id TEXT, project_id TEXT)")
    con.executemany("INSERT INTO projects VALUES (?,?,?)", projects)
    con.executemany("INSERT INTO tasks VALUES (?,?)", tasks)
    con.commit()
    con.close()


def launch_line(**kw):
    row = {
        "bot_id": "bot", "task": "task", "tool": "claude", "cwd": "/tmp/work",
        "started_at": "2026-10-02T11:00:00+00:00", "pid": 1, "session_id": None,
    }
    row.update(kw)
    return json.dumps(row) + "\n"


class LaunchCollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "agent-data")
        self.codex = os.path.join(self.tmp.name, "codex")
        self.grok = os.path.join(self.tmp.name, "grok")
        self.db_path = os.path.join(self.tmp.name, "quota.db")
        self.panel = os.path.join(self.tmp.name, "panel.html")
        self.launches = os.path.join(self.tmp.name, "launches.jsonl")
        os.makedirs(self.root)
        self._saved_env = {key: os.environ.get(key) for key in (
            "QUOTA_ATTRIBUTION_PLUGIN", "QUOTA_TASKS_DB", "QUOTA_REPORTS_GLOB")}
        os.environ["QUOTA_ATTRIBUTION_PLUGIN"] = "quota.plugins.sqlite_tasks:lookup"
        os.environ["QUOTA_TASKS_DB"] = os.path.join(self.root, "tasks.db")
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
                          "--launches", self.launches, "--backfill", *extra])
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

    def test_session_overrides_plugin_and_cursor_is_incremental(self):
        write_tasks(self.root, [("project-alpha", "Project Alpha", ALPHA)], [("JOB-124", "project-alpha")])
        write_claude(self.root, "JOB-124", "sess-override", "2026-10-02T12:00:00Z")
        self.collect()
        run = self.row("SELECT * FROM cli_runs WHERE session_id='sess-override'")
        self.assertEqual(run["bot_id"], ALPHA)
        self.assertEqual(run["task"], "JOB-124")
        self.assertEqual(run["attribution_method"], "plugin")
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_launches"), 0)

        with open(self.launches, "w", encoding="utf-8") as f:
            f.write(launch_line(
                bot_id=BETA, task="wrapped-task", tool="claude", cwd="/tmp/somewhere",
                started_at="2026-09-01T00:00:00+00:00", pid=42, session_id="sess-override",
                prompt="CANARY-LAUNCH-PROMPT", args=["CANARY-LAUNCH-PROMPT"]))
            f.write(launch_line(
                bot_id=OTHER, task="nope", tool="codex", cwd="/tmp/somewhere",
                started_at="2026-10-02T12:00:00+00:00", pid=7, session_id="sess-override"))
        self.collect("--report")
        run = self.row("SELECT * FROM cli_runs WHERE session_id='sess-override'")
        self.assertEqual(run["bot_id"], BETA)
        self.assertEqual(run["task"], "wrapped-task")
        self.assertEqual(run["attribution_method"], "launch_session")
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_runs"), 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_launches"), 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM events"), 0)
        with open(self.db_path, "rb") as f:
            self.assertNotIn(b"CANARY-LAUNCH-PROMPT", f.read())
        with open(self.panel, encoding="utf-8") as f:
            panel = f.read()
        self.assertIn("attribution: launch_session 1, launch_cwd 0, plugin 0, unattributed 0", panel)

        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_launches"), 2)
        self.assertEqual(self.one("SELECT backlog_bytes FROM cursors WHERE source_id='launches.jsonl'"), 0)
        self.assertEqual(self.one(
            "SELECT attribution_method FROM cli_runs WHERE session_id='sess-override'"), "launch_session")

        with open(self.launches, "a", encoding="utf-8") as f:
            f.write('{"bot_id":')
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_launches"), 2)
        self.assertGreater(self.one("SELECT backlog_bytes FROM cursors WHERE source_id='launches.jsonl'"), 0)

        with open(self.launches, "a", encoding="utf-8") as f:
            f.write('"z","task":"t2","tool":"grok","cwd":"/tmp/no-such",'
                    '"started_at":"2026-10-02T00:00:00+00:00","pid":3,"session_id":null}\n')
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_launches"), 3)
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_launches"), 3)
        self.assertEqual(self.one("SELECT backlog_bytes FROM cursors WHERE source_id='launches.jsonl'"), 0)

    def test_cwd_time_fallback_consumes_nearest_launch(self):
        write_tasks(self.root, [
            ("project-demo", "Project Demo", BETA),
            ("project", "Project", OTHER),
        ])
        write_codex(self.codex, "early", "codex-early", "/work/project-demo-job-195", "2026-10-02T12:00:00Z")
        write_codex(self.codex, "late", "codex-late", "/work/project-demo-job-195", "2026-10-02T18:00:00Z")
        write_codex(self.codex, "eq", "codex-eq", "/work/boundary-eq", "2026-10-03T12:00:00Z")
        write_codex(self.codex, "over", "codex-over", "/work/boundary-late", "2026-10-03T12:00:00Z")
        write_claude(self.root, "named-task", "sess-named", "2026-10-02T15:00:00Z")
        with open(self.launches, "w", encoding="utf-8") as f:
            f.write(launch_line(
                bot_id="bot-near", task="tagged-near", tool="codex",
                cwd="/work/project-demo-job-195", started_at="2026-10-02T11:00:00+00:00",
                session_id=None))
            f.write(launch_line(
                bot_id="bot-old", task="tagged-old", tool="codex",
                cwd="/work/project-demo-job-195", started_at="2026-10-01T10:00:00+00:00",
                session_id=None))
            f.write(launch_line(
                bot_id="bot-other", task="tagged-other", tool="codex",
                cwd="/tmp/other-place", started_at="2026-10-02T11:30:00+00:00", session_id=None))
            f.write(launch_line(
                bot_id="bot-eq", task="tagged-eq", tool="codex",
                cwd="/work/boundary-eq", started_at="2026-10-02T12:00:00+00:00", session_id=None))
            f.write(launch_line(
                bot_id="bot-over", task="tagged-over", tool="codex",
                cwd="/work/boundary-late", started_at="2026-10-02T11:59:59+00:00", session_id=None))
            f.write(launch_line(
                bot_id="bot-named", task="named-task", tool="claude",
                cwd="/tmp/does-not-match", started_at="2026-10-02T14:00:00+00:00", session_id=None))
        self.collect("--report")

        early = self.row("SELECT * FROM cli_runs WHERE session_id='codex-early'")
        self.assertEqual(early["attribution_method"], "launch_cwd")
        self.assertEqual(early["bot_id"], "bot-near")
        self.assertEqual(early["task"], "tagged-near")
        self.assertEqual(early["cwd"], "/work/project-demo-job-195")

        late = self.row("SELECT * FROM cli_runs WHERE session_id='codex-late'")
        self.assertEqual(late["attribution_method"], "plugin")
        self.assertEqual(late["bot_id"], BETA)  # launch already consumed by the nearer run
        self.assertEqual(late["task"], "JOB-195")

        eq = self.row("SELECT * FROM cli_runs WHERE session_id='codex-eq'")
        self.assertEqual(eq["attribution_method"], "launch_cwd")
        self.assertEqual(eq["bot_id"], "bot-eq")

        over = self.row("SELECT * FROM cli_runs WHERE session_id='codex-over'")
        self.assertEqual(over["attribution_method"], "unattributed")
        self.assertEqual(over["task"], "boundary-late")

        named = self.row("SELECT * FROM cli_runs WHERE session_id='sess-named'")
        self.assertEqual(named["attribution_method"], "launch_cwd")
        self.assertEqual(named["bot_id"], "bot-named")
        self.assertEqual(named["task"], "named-task")

        with open(self.panel, encoding="utf-8") as f:
            panel = f.read()
        self.assertIn("attribution: launch_session 0, launch_cwd 3, plugin 1, unattributed 1", panel)

        n = self.one("SELECT COUNT(*) FROM cli_launches")
        self.collect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM cli_launches"), n)
        self.assertEqual(self.one("SELECT attribution_method FROM cli_runs WHERE session_id='codex-early'"),
                         "launch_cwd")

    def test_old_cli_runs_migration_is_idempotent(self):
        bare = os.path.join(self.tmp.name, "old.db")
        con = sqlite3.connect(bare)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "CREATE TABLE cli_runs ("
            "source_id TEXT NOT NULL, run_key TEXT PRIMARY KEY, tool TEXT NOT NULL, task TEXT,"
            "bot_id TEXT NOT NULL DEFAULT 'unattributed', session_id TEXT, ts TEXT, model TEXT,"
            "num_turns INTEGER, duration_ms INTEGER, is_error INTEGER, cost_usd REAL,"
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
            "cache_create_tokens INTEGER, reasoning_tokens INTEGER, subagent_count INTEGER, raw_json TEXT)")
        con.execute("INSERT INTO cli_runs (source_id, run_key, tool, task, bot_id) VALUES ('s','k','claude','t','b')")
        con.commit()
        con.close()
        con = collect.open_db(bare)
        cols = {r[1] for r in con.execute("PRAGMA table_info(cli_runs)")}
        self.assertIn("cwd", cols)
        self.assertIn("attribution_method", cols)
        self.assertIn("cost_usd_est", cols)
        self.assertIn("cost_estimated", cols)
        self.assertIn("model_defaulted", cols)
        self.assertEqual(tuple(con.execute("SELECT task, bot_id FROM cli_runs WHERE run_key='k'").fetchone()), ("t", "b"))
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='cli_launches'").fetchone()[0], 1)
        con.close()
        con = collect.open_db(bare)
        con.execute("SELECT cwd, attribution_method FROM cli_runs")
        con.execute("SELECT COUNT(*) FROM cli_launches")
        self.assertEqual(con.execute("SELECT bot_id FROM cli_runs WHERE run_key='k'").fetchone()[0], "b")
        con.close()


class LaunchPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.root = os.path.join(self.tmp.name, "root")
        os.makedirs(self.home)
        os.makedirs(self.root)
        self.db_path = os.path.join(self.tmp.name, "quota.db")
        self._saved = {key: os.environ.get(key) for key in ("HOME", "GROKBOT_QUOTA_STATE")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def use_home(self, **env):
        os.environ["HOME"] = self.home
        os.environ.pop("GROKBOT_QUOTA_STATE", None)
        os.environ.update(env)

    def write_launch(self, path, bot):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(launch_line(bot_id=bot, session_id=bot))

    def collect(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = collect.main([
                "--db", self.db_path, "--root", self.root,
                "--codex-sessions", os.path.join(self.tmp.name, "codex"),
                "--grok-sessions", os.path.join(self.tmp.name, "grok"),
                "--quiet",
            ])
        return code, err.getvalue()

    def bots(self):
        con = sqlite3.connect(self.db_path)
        try:
            return {row[0] for row in con.execute("SELECT bot_id FROM cli_launches")}
        finally:
            con.close()

    def test_specs_follow_env_or_both_defaults(self):
        os.environ.pop("GROKBOT_QUOTA_STATE", None)
        specs = collect.default_launches_specs()
        self.assertEqual([sid for _path, sid in specs], ["launches.jsonl", "share/launches.jsonl"])
        self.assertTrue(specs[0][0].endswith(os.path.join(".local", "state", "grokbot-quota", "launches.jsonl")))
        self.assertTrue(specs[1][0].endswith(os.path.join(".local", "share", "grokbot-quota", "launches.jsonl")))
        self.assertTrue(collect.default_launches_path().endswith(
            os.path.join(".local", "state", "grokbot-quota", "launches.jsonl")))
        extra = os.path.join(self.tmp.name, "extra")
        os.environ["GROKBOT_QUOTA_STATE"] = extra
        self.assertEqual(
            collect.default_launches_specs(),
            [(os.path.join(extra, "launches.jsonl"), "launches.jsonl")])
        self.assertEqual(collect.default_launches_path(), os.path.join(extra, "launches.jsonl"))

    def test_reads_state_and_share_when_env_unset(self):
        self.use_home()
        state = os.path.join(self.home, ".local", "state", "grokbot-quota", "launches.jsonl")
        share = os.path.join(self.home, ".local", "share", "grokbot-quota", "launches.jsonl")
        self.write_launch(state, "from-state")
        self.write_launch(share, "from-share")
        code, err = self.collect()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bots(), {"from-state", "from-share"})
        con = sqlite3.connect(self.db_path)
        sids = {row[0] for row in con.execute("SELECT DISTINCT source_id FROM cli_launches")}
        con.close()
        self.assertEqual(sids, {"launches.jsonl", "share/launches.jsonl"})

    def test_env_selects_one_launches_file(self):
        extra = os.path.join(self.tmp.name, "extra")
        self.write_launch(os.path.join(extra, "launches.jsonl"), "from-env")
        self.write_launch(os.path.join(self.home, ".local", "state", "grokbot-quota", "launches.jsonl"), "from-state")
        self.write_launch(os.path.join(self.home, ".local", "share", "grokbot-quota", "launches.jsonl"), "from-share")
        self.use_home(GROKBOT_QUOTA_STATE=extra)
        code, err = self.collect()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bots(), {"from-env"})

    def test_missing_default_files_are_not_sources(self):
        self.use_home()
        code, err = self.collect()
        self.assertEqual(code, 0, err)
        con = sqlite3.connect(self.db_path)
        n = con.execute("SELECT COUNT(*) FROM sources WHERE source_type='cli_launches'").fetchone()[0]
        con.close()
        self.assertEqual(n, 0)

    def test_unreadable_launches_recorded_missing(self):
        self.use_home()
        state = os.path.join(self.home, ".local", "state", "grokbot-quota", "launches.jsonl")
        share = os.path.join(self.home, ".local", "share", "grokbot-quota", "launches.jsonl")
        self.write_launch(state, "from-state")
        self.write_launch(share, "from-share")
        os.chmod(share, 0)
        real_open = os.open
        patched = False
        try:
            fd = os.open(share, os.O_RDONLY)
        except PermissionError:
            pass
        else:
            os.close(fd)
            os.chmod(share, 0o644)

            def guarded(path, flags, *args, **kwargs):
                if os.path.abspath(path) == os.path.abspath(share):
                    raise PermissionError(errno.EACCES, "Permission denied", path)
                return real_open(path, flags, *args, **kwargs)

            os.open = guarded
            patched = True
        try:
            code, err = self.collect()
        finally:
            if patched:
                os.open = real_open
            try:
                os.chmod(share, 0o644)
            except OSError:
                pass
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bots(), {"from-state"})
        con = sqlite3.connect(self.db_path)
        status = dict(con.execute(
            "SELECT source_id, status FROM sources WHERE source_type='cli_launches'"))
        errors = con.execute("SELECT errors FROM collector_runs").fetchone()[0]
        con.close()
        self.assertEqual(status.get("share/launches.jsonl"), "missing")
        self.assertEqual(status.get("launches.jsonl"), "ok")
        self.assertEqual(errors, 0)
        code, err = self.collect()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bots(), {"from-state", "from-share"})
        con = sqlite3.connect(self.db_path)
        status = con.execute(
            "SELECT status FROM sources WHERE source_id='share/launches.jsonl'").fetchone()[0]
        con.close()
        self.assertEqual(status, "ok")


class GrokbotRunFallbackTest(GrokbotRunTest):
    def _run(self, home, args, extra_env=None):
        env = os.environ.copy()
        env.pop("GROKBOT_QUOTA_STATE", None)
        env["HOME"] = home
        env["GROKBOT_RUN_TOOL_CLAUDE"] = self.fake
        env["GROKBOT_RUN_TOOL_CODEX"] = self.fake
        env["GROKBOT_RUN_TOOL_GROK"] = self.fake
        if extra_env:
            env.update(extra_env)
        return subprocess.run([LAUNCHER, *args], env=env, cwd=self.work,
                              capture_output=True, text=True)

    def test_unwritable_state_falls_back_to_share_and_execs(self):
        home = os.path.join(self.tmp.name, "home-fallback")
        local = os.path.join(home, ".local")
        os.makedirs(local)
        with open(os.path.join(local, "state"), "w", encoding="utf-8") as f:
            f.write("not-a-directory")
        proc = self._run(home, ["bot-9", "job-9", "--", "grok", "CANARY-PROMPT"])
        self.assertEqual(proc.returncode, 7, proc.stderr)
        share = os.path.join(home, ".local", "share", "grokbot-quota", "launches.jsonl")
        self.assertTrue(os.path.isfile(share))
        with open(share, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("CANARY-PROMPT", raw)
        self.assertEqual(json.loads(raw)["bot_id"], "bot-9")
        self.assertEqual(json.loads(raw)["tool"], "grok")

    def test_writable_state_does_not_use_share(self):
        home = os.path.join(self.tmp.name, "home-ok")
        os.makedirs(home)
        proc = self._run(home, ["bot-8", "job-8", "--", "claude", "-p", "x"])
        self.assertEqual(proc.returncode, 7, proc.stderr)
        state = os.path.join(home, ".local", "state", "grokbot-quota", "launches.jsonl")
        share = os.path.join(home, ".local", "share", "grokbot-quota", "launches.jsonl")
        self.assertTrue(os.path.isfile(state))
        self.assertFalse(os.path.exists(share))

    def test_explicit_env_does_not_fall_back(self):
        home = os.path.join(self.tmp.name, "home-env")
        os.makedirs(home)
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        proc = self._run(home, ["bot-7", "job-7", "--", "codex"],
                         {"GROKBOT_QUOTA_STATE": os.path.join(blocker, "child")})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("grokbot-run", proc.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(home, ".local", "share", "grokbot-quota", "launches.jsonl")))

    def test_both_locations_unwritable_does_not_exec(self):
        home = os.path.join(self.tmp.name, "home-both")
        local = os.path.join(home, ".local")
        os.makedirs(local)
        for name in ("state", "share"):
            with open(os.path.join(local, name), "w", encoding="utf-8") as f:
                f.write("x")
        proc = self._run(home, ["bot-6", "job-6", "--", "grok"])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()

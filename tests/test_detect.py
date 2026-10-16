"""Path defaults, source detection, and attribution staying off."""
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quota import collect, detect, paths  # noqa: E402
from quota.plugins import load_config  # noqa: E402

ENV_KEYS = (
    "HOME", "QUOTA_DATA_ROOT", "QUOTA_DB", "QUOTA_OUT", "QUOTA_REPORTS_GLOB",
    "QUOTA_CODEX_SESSIONS", "QUOTA_GROK_SESSIONS", "QUOTA_ATTRIBUTION_PLUGIN",
    "QUOTA_TASKS_DB", "QUOTA_CONFIG", "GROKBOT_QUOTA_STATE",
)


class EnvGuard(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class DetectTest(EnvGuard):
    def test_counts_sources_without_writing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = os.path.join(tmp.name, "home")
        root = os.path.join(home, "agent-data")
        os.makedirs(os.path.join(root, "agents", "bot-alpha"))
        os.makedirs(os.path.join(root, "agent-transcripts", "bot-alpha"))
        os.makedirs(os.path.join(root, "reports", "JOB-1"))
        codex = os.path.join(tmp.name, "codex", "2026", "10", "02")
        grok = os.path.join(tmp.name, "grok", "demo", "sess-1")
        state = os.path.join(tmp.name, "state")
        os.makedirs(codex)
        os.makedirs(grok)
        os.makedirs(state)
        blobs = {
            os.path.join(root, "search-index.db"): b"si",
            os.path.join(root, "agents", "bot-alpha", "audit.jsonl"): b"audit\n",
            os.path.join(root, "agents", "bot-alpha", "store.db"): b"store",
            os.path.join(root, "agent-transcripts", "bot-alpha", "bot-alpha.jsonl"): b"tr",
            os.path.join(root, "reports", "JOB-1", "claude-1.jsonl"): b"cl\n",
            os.path.join(codex, "rollout-1.jsonl"): b"cx",
            os.path.join(grok, "usage.json"): b"gk",
            os.path.join(state, "launches.jsonl"): b"ln\n",
        }
        for path, data in blobs.items():
            with open(path, "wb") as f:
                f.write(data)
        os.environ["HOME"] = home
        os.environ["QUOTA_DATA_ROOT"] = root
        os.environ["QUOTA_CODEX_SESSIONS"] = os.path.join(tmp.name, "codex")
        os.environ["QUOTA_GROK_SESSIONS"] = os.path.join(tmp.name, "grok")
        os.environ["GROKBOT_QUOTA_STATE"] = state

        def snap():
            found = {}
            for dirpath, _dirs, files in os.walk(tmp.name):
                for name in files:
                    path = os.path.join(dirpath, name)
                    st = os.stat(path)
                    found[path] = (st.st_size, st.st_mtime_ns)
            return found

        before = snap()
        text = detect.report()
        self.assertEqual(snap(), before)
        self.assertIn(f"agent-data root: PRESENT {root}", text)
        self.assertIn("search-index.db: 1 file, 2 bytes", text)
        self.assertIn("agents/audit.jsonl: 1 file, 6 bytes", text)
        self.assertIn("agents/store.db: 1 file, 5 bytes", text)
        self.assertIn("agent-transcripts: 1 file, 2 bytes", text)
        self.assertIn("claude reports: 1 file, 3 bytes", text)
        self.assertIn("codex sessions: 1 file, 2 bytes", text)
        self.assertIn("grok sessions: 1 file, 2 bytes", text)
        self.assertIn("launches.jsonl: 1 file, 3 bytes", text)

    def test_absent_root_is_explicit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["HOME"] = os.path.join(tmp.name, "home")
        os.environ["QUOTA_CODEX_SESSIONS"] = os.path.join(tmp.name, "no-codex")
        os.environ["QUOTA_GROK_SESSIONS"] = os.path.join(tmp.name, "no-grok")
        os.environ["GROKBOT_QUOTA_STATE"] = os.path.join(tmp.name, "state")
        text = detect.report()
        self.assertIn("agent-data root: ABSENT", text)
        self.assertIn("search-index.db: 0 files, 0 bytes", text)
        self.assertIn("claude reports: 0 files, 0 bytes", text)
        self.assertIn("launches.jsonl: 0 files, 0 bytes", text)


class PathDefaultTest(EnvGuard):
    def test_env_overrides_and_state_fallback(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["HOME"] = os.path.join(tmp.name, "home")
        os.environ["QUOTA_DATA_ROOT"] = os.path.join(tmp.name, "extra-root")
        os.makedirs(os.environ["QUOTA_DATA_ROOT"])
        self.assertEqual(paths.default_data_root(), os.environ["QUOTA_DATA_ROOT"])
        os.environ["QUOTA_DB"] = os.path.join(tmp.name, "q.db")
        self.assertEqual(paths.default_db(), os.environ["QUOTA_DB"])
        os.environ["QUOTA_OUT"] = os.path.join(tmp.name, "dash.html")
        self.assertEqual(paths.default_out(), os.environ["QUOTA_OUT"])
        os.environ.pop("QUOTA_DB", None)
        os.environ.pop("QUOTA_DATA_ROOT", None)
        # State cannot be created when ~/.local/state is a file.
        local = os.path.join(os.environ["HOME"], ".local")
        os.makedirs(local)
        with open(os.path.join(local, "state"), "w", encoding="utf-8") as f:
            f.write("x")
        self.assertTrue(str(paths.state_dir()).endswith(
            os.path.join(".local", "share", "grokbot-quota")))
        self.assertTrue(paths.default_db().endswith(
            os.path.join(".local", "share", "grokbot-quota", "quota.db")))

    def test_shipped_plugins_are_off(self):
        cfg = load_config()
        self.assertEqual(cfg.get("attribution_plugins"), [])


class PluginOffTest(EnvGuard):
    def test_tasks_db_is_ignored_until_enabled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "root")
        os.makedirs(os.path.join(root, "reports", "JOB-1"))
        with open(os.path.join(root, "reports", "JOB-1", "claude-1.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "result", "session_id": "s", "total_cost_usd": 1,
                                "num_turns": 1, "is_error": False, "usage": {}}) + "\n")
        tasks = os.path.join(root, "tasks.db")
        con = sqlite3.connect(tasks)
        con.execute("CREATE TABLE projects (id TEXT, name TEXT, bot_id TEXT)")
        con.execute("CREATE TABLE tasks (id TEXT, project_id TEXT)")
        con.execute("INSERT INTO tasks VALUES ('JOB-1', 'project-demo')")
        con.execute("INSERT INTO projects VALUES ('project-demo', 'Project Demo', 'bot-alpha')")
        con.commit()
        con.close()
        with open(tasks, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        db_path = os.path.join(tmp.name, "quota.db")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            collect.main([
                "--db", db_path, "--root", root, "--backfill",
                "--codex-sessions", os.path.join(tmp.name, "codex"),
                "--grok-sessions", os.path.join(tmp.name, "grok"),
                "--launches", os.path.join(tmp.name, "launches.jsonl"),
            ])
        con = sqlite3.connect(db_path)
        row = con.execute("SELECT bot_id, attribution_method FROM cli_runs").fetchone()
        con.close()
        self.assertEqual(tuple(row), ("unattributed", "unattributed"))
        with open(tasks, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest)
        self.assertFalse(os.path.exists(tasks + "-wal"))

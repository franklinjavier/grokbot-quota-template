"""Anonymizer: stable stand-ins, synthetic rows only."""
import io
import os
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

from quota import anonymize, dashboard, leakguard  # noqa: E402

SCHEMA = Path(ROOT, "quota", "schema.sql").read_text(encoding="utf-8")
ALPINE = "00000000-0000-4000-8000-0000000000c1"
BIRCH = "00000000-0000-4000-8000-0000000000c2"


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    return db


def add_bot(db, eid, name, slug):
    db.execute(
        "INSERT INTO entities (entity_id, kind, display_name, slug, profile_present) "
        "VALUES (?, 'bot', ?, ?, 1)",
        (eid, name, slug))


class AnonymizeTest(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.addCleanup(self.db.close)
        add_bot(self.db, BIRCH, "Birch", "birch")
        add_bot(self.db, ALPINE, "Alpine", "alpine")
        when = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, task, bot_id, session_id, ts, "
            "cost_usd, cwd) VALUES (?,?,?,?,?,?,?,?,?)",
            ("cli/one", "run-alpine", "claude", "job-north", ALPINE, ALPINE, when, 3.5,
             "/srv/lab/widget"))
        self.db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, task, bot_id, ts, cost_usd) "
            "VALUES (?,?,?,?,?,?,?)",
            ("cli/two", "run-birch", "grok", "job-south", BIRCH, when, 1.25))
        token = leakguard.person_names()[0]
        self.person = token
        self.db.execute(
            "INSERT INTO routines (owner_id, routine_id, name, enabled, schedule) "
            "VALUES (?,?,?,?,?)",
            (ALPINE, "weekly-sweep", "Sweep for " + token, 1, "15 8 * * 1-5"))
        self.mail = "dev" + "@" + "example.test"
        self.db.execute(
            "INSERT INTO sources (source_id, source_type, entity_id, status) VALUES (?,?,?,?)",
            ("notes/" + self.mail + "/a.jsonl", "audit", ALPINE, "ok"))
        self.db.commit()

    def test_labels_are_stable_and_phonetic(self):
        anon = anonymize.Anonymizer.from_db(self.db)
        again = anonymize.Anonymizer.from_db(self.db)
        self.assertEqual(anon.rewrite("Alpine", "display_name"), "bot-alpha")
        self.assertEqual(anon.rewrite("Birch", "display_name"), "bot-bravo")
        self.assertEqual(anon.rewrite("Alpine", "display_name"), again.rewrite("Alpine", "display_name"))
        self.assertEqual(anon.rewrite(ALPINE), again.rewrite(ALPINE))
        self.assertNotEqual(anon.rewrite(ALPINE), ALPINE)
        self.assertNotEqual(anon.rewrite(ALPINE), anon.rewrite(BIRCH))
        self.assertTrue(anon.rewrite(ALPINE).startswith("11111111-1111-4111-8111-"))
        self.assertEqual(anon.rewrite("job-north", "task"), "task-alpha")
        self.assertEqual(anon.rewrite("job-south", "task"), "task-bravo")
        self.assertEqual(anon.rewrite("grok"), "grok")
        # A bot whose label is the tool token still gets a stand-in. The tool does not.
        self.db.execute(
            "INSERT INTO entities (entity_id, kind, display_name, slug, profile_present) "
            "VALUES (?, 'bot', ?, ?, 1)",
            ("00000000-0000-4000-8000-0000000000c3", "Grok", "grok"))
        named = anonymize.Anonymizer.from_db(self.db)
        self.assertEqual(named.rewrite("Grok", "display_name"), "bot-charlie")
        self.assertEqual(named.rewrite("grok"), "grok")
        self.assertTrue(anon.rewrite("/srv/lab/widget", "cwd").startswith("/home/user/"))
        self.assertNotIn("/srv/lab", anon.rewrite("/srv/lab/widget", "cwd"))

    def test_spans_cover_mail_hosts_people_and_prefixes(self):
        anon = anonymize.Anonymizer.from_db(self.db)
        self.assertEqual(anon.rewrite("ping " + self.mail), "ping " + "user@" + "example.com")
        self.assertNotIn(self.person.casefold(), anon.rewrite("hello " + self.person).casefold())
        self.assertIn("user", anon.rewrite("hello " + self.person))
        suffix = leakguard.host_suffix()
        host = "desk-1" + suffix
        rewritten = anon.rewrite("see " + host + " now")
        self.assertNotIn(suffix, rewritten)
        self.assertIn("example.com", rewritten)
        token = leakguard.grok_bot_token()
        rewritten = anon.rewrite("box " + token + "-1")
        self.assertNotIn(token, rewritten)
        for word in leakguard.fixed_substrings():
            if not word.startswith("/"):
                continue
            out = anon.rewrite(word + "/proj")
            self.assertNotIn(word, out)
            self.assertTrue(out.startswith("/home/user"), out)

    def test_copy_rewrites_rows_and_leaves_the_source(self):
        anon = anonymize.Anonymizer.from_db(self.db)
        view = anon.writable_copy(self.db)
        self.addCleanup(view.close)
        names = dict(view.execute("SELECT display_name, entity_id FROM entities"))
        # display_name was the real label; the copy stores the stand-in.
        stored = {row[0] for row in view.execute("SELECT display_name FROM entities")}
        self.assertEqual(stored, {"bot-alpha", "bot-bravo"})
        tools = {row[0] for row in view.execute("SELECT tool FROM cli_runs")}
        self.assertEqual(tools, {"claude", "grok"})
        tasks = {row[0] for row in view.execute("SELECT task FROM cli_runs")}
        self.assertEqual(tasks, {"task-alpha", "task-bravo"})
        cwd = view.execute("SELECT cwd FROM cli_runs WHERE cwd IS NOT NULL").fetchone()[0]
        self.assertTrue(cwd.startswith("/home/user/"))
        routine = view.execute("SELECT name FROM routines").fetchone()[0]
        self.assertEqual(routine, "routine-alpha")
        source = view.execute("SELECT source_id FROM sources").fetchone()[0]
        self.assertNotIn(self.mail, source)
        self.assertNotIn(ALPINE, source)
        original = self.db.execute("SELECT display_name FROM entities ORDER BY display_name").fetchall()
        self.assertEqual([row[0] for row in original], ["Alpine", "Birch"])
        self.assertTrue(names)

    def test_dashboard_writes_only_when_the_guard_passes(self):
        # File database so the CLI can reopen it read-only.
        path = Path(os.environ.get("TMPDIR", "/tmp")) / ("anon-" + ALPINE[-4:] + ".db")
        if path.exists():
            path.unlink()
        file_db = sqlite3.connect(path)
        file_db.executescript(SCHEMA)
        add_bot(file_db, ALPINE, "Alpine", "alpine")
        when = datetime.now(timezone.utc).isoformat()
        file_db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, task, bot_id, ts, cost_usd, cwd) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("cli/one", "run-1", "claude", "job-north", ALPINE, when, 3.5, "/srv/lab/widget"))
        file_db.execute("INSERT INTO usage_readings VALUES (?,?,?,?)",
                        (when[:10], 40, "Alpine /srv/lab/widget", 1792148400000))
        file_db.execute("INSERT INTO cursors (source_id, generation) VALUES ('test', 0)")
        file_db.execute(
            "INSERT INTO events (source_id, source_key, event_type, time_basis, entity_id, "
            "root_entity_id, est_day) VALUES ('test', '1', 'inference_step', 'exact', ?, ?, ?)",
            (ALPINE, ALPINE, when[:10]))
        file_db.commit()
        file_db.execute("PRAGMA wal_checkpoint(FULL)")
        file_db.close()
        self.addCleanup(path.unlink, missing_ok=True)
        out = path.with_suffix(".html")
        self.addCleanup(out.unlink, missing_ok=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dashboard.main(["--db", str(path), "--out", str(out), "--anonymize"])
        self.assertEqual(code, 0, buf.getvalue())
        text = out.read_text(encoding="utf-8")
        self.assertIn("bot-alpha", text)
        self.assertIn('title="bot-alpha: 40.0%"', text)
        self.assertNotIn("Alpine", text)
        self.assertNotIn("job-north", text)
        self.assertNotIn("/srv/lab/widget", text)
        self.assertIn("$3.50", text)
        check = sqlite3.connect(path)
        try:
            self.assertEqual(leakguard.find_hits(check, text), [])
        finally:
            check.close()
        still = sqlite3.connect(path)
        self.assertEqual(still.execute("SELECT display_name FROM entities").fetchone()[0], "Alpine")
        still.close()

        blocked = path.with_suffix(".blocked.html")
        with mock.patch("quota.leakguard.find_hits", return_value=["planted-name"]):
            code = dashboard.main(["--db", str(path), "--out", str(blocked), "--anonymize"])
        self.assertEqual(code, 1)
        self.assertFalse(blocked.exists())


if __name__ == "__main__":
    unittest.main()

"""Leak guard: synthetic identifiers only."""
import io
import os
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

from quota import leakguard  # noqa: E402

SCHEMA = Path(ROOT, "quota", "schema.sql").read_text(encoding="utf-8")
AGENT = "00000000-0000-4000-8000-0000000000d4"


def fresh_file(folder: Path) -> Path:
    path = folder / "quota.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute(
        "INSERT INTO entities (entity_id, kind, display_name, slug, profile_present) "
        "VALUES (?, 'bot', ?, ?, 1)",
        (AGENT, "Alpine", "alpine"))
    db.execute(
        "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, cwd) VALUES (?,?,?,?,?)",
        ("cli/one", "run-1", "claude", AGENT, "/srv/lab/widget"))
    db.commit()
    db.execute("PRAGMA wal_checkpoint(FULL)")
    db.close()
    return path


class LeakguardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "leakguard-tests"
        self.tmp.mkdir(exist_ok=True)
        self.db_path = fresh_file(self.tmp)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in ("quota.db", "quota.db-wal", "quota.db-shm"):
            path = self.tmp / name
            if path.exists():
                path.unlink()

    def needles(self):
        db = leakguard.open_ro(str(self.db_path))
        try:
            return leakguard.collect_needles(db)
        finally:
            db.close()

    def test_collects_names_ids_and_paths(self):
        needles = self.needles()
        self.assertIn("Alpine", needles.names)
        self.assertIn("alpine", [name.casefold() for name in needles.names])
        self.assertIn(AGENT.casefold(), needles.uuids)
        self.assertTrue(any("/srv/lab/widget" in path for path in needles.paths))
        self.assertNotIn("claude", [name.casefold() for name in needles.names])

    def test_hits_and_word_boundaries(self):
        needles = self.needles()
        self.assertIn("Alpine", leakguard.hits_in_text("owner Alpine", needles))
        self.assertIn(AGENT, leakguard.hits_in_text("id " + AGENT, needles))
        self.assertIn("/srv/lab/widget", leakguard.hits_in_text("cwd /srv/lab/widget", needles))
        self.assertEqual(leakguard.hits_in_text("bot-alpha spent $1.00", needles), [])

        short = leakguard.Needles()
        short.add_name("Jo")
        self.assertEqual(leakguard.hits_in_text("Join the group", short), [])
        self.assertTrue(leakguard.hits_in_text("Hello Jo.", short))

    def test_fixed_patterns_mail_and_hosts(self):
        empty = leakguard.Needles()
        word = leakguard.fixed_substrings()[0]
        self.assertIn(word, leakguard.hits_in_text("xx " + word + " yy", empty))
        addr = "dev" + "@" + "example.test"
        hits = leakguard.hits_in_text("wrote " + addr, empty)
        self.assertTrue(any("@" in hit for hit in hits))
        suffix = leakguard.host_suffix()
        host = "desk-1" + suffix
        self.assertTrue(any(suffix in hit for hit in leakguard.hits_in_text(host, empty)))
        token = leakguard.grok_bot_token()
        self.assertTrue(any(token in hit for hit in leakguard.hits_in_text(token + "-1", empty)))
        other = "00000000-0000-4000-8000-0000000000ee"
        self.assertEqual(leakguard.hits_in_text(other, empty), [])

    def test_cli_lists_matches_and_exits_nonzero(self):
        needles_file = self.tmp / "note.txt"
        needles_file.write_text("Alpine keeps /srv/lab/widget\n", encoding="utf-8")
        clean = self.tmp / "clean.txt"
        clean.write_text("bot-alpha spent $1.00\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = leakguard.main(["--db", str(self.db_path), str(clean)])
        self.assertEqual(code, 0, buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = leakguard.main(["--db", str(self.db_path), str(needles_file), str(clean)])
        self.assertEqual(code, 1)
        self.assertIn("Alpine", buf.getvalue())
        self.assertIn("/srv/lab/widget", buf.getvalue())
        missing = leakguard.main(["--db", str(self.db_path), str(self.tmp / "missing.txt")])
        self.assertEqual(missing, 2)


if __name__ == "__main__":
    unittest.main()

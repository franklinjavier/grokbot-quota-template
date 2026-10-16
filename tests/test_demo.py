"""Fake demo database: deterministic, fixture ids only, under the budget."""
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

from quota import budgets, demo  # noqa: E402

TODAY = date(2026, 10, 15)
UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
FAKE = re.compile(r"(?i)^00000000-0000-4000-8000-0000000000[0-9a-f]{2}$")
NAMES = ("Atlas", "Juniper", "Pixel", "Nova", "Orbit", "Maple", "Comet", "Quill")


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def render(self, seed, today=TODAY, agents=None):
        self._n = getattr(self, "_n", 0) + 1
        folder = Path(self.tmp.name) / f"run-{self._n}"
        folder.mkdir()
        db_path = folder / "quota.db"
        out_path = folder / "dashboard.html"
        html = demo.build(db_path, out_path, seed=seed, today=today, agents=agents)
        self.assertEqual(out_path.read_text(encoding="utf-8"), html)
        return html, db_path

    def test_same_seed_matches_and_other_seed_differs(self):
        first, _db1 = self.render(1)
        second, _db2 = self.render(1)
        other, _db3 = self.render(2)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertIn('lang="en"', first)
        self.assertIn('42%</span> of plan used', first)
        self.assertIn("<strong>", first)
        self.assertRegex(first, r"<strong>[0-9]{1,3}(?:,[0-9]{3})+</strong> activity points")
        self.assertRegex(first, r"\$[0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2}")

    def test_fake_ids_svg_avatars_and_english_labels(self):
        agents = Path(self.tmp.name) / "agents"
        html, db_path = self.render(1, agents=agents)
        pngs = list(agents.rglob("*.png"))
        self.assertEqual(pngs, [])
        self.assertNotIn("data:image/png", html)
        self.assertGreaterEqual(html.count('<svg class="avatar"'), 8)
        for name in NAMES:
            self.assertIn(name, html)
        self.assertIn("JOB-1", html)
        self.assertIn("CLI spend this week", html)
        self.assertIn("cost (USD)", html)
        self.assertIn("Unattributed", html)
        self.assertIn("Weekday digest", html)
        self.assertIn("on weekdays", html)
        self.assertIn("paused", html)
        self.assertIn("(estimated)", html)
        self.assertNotIn("EXCEEDED", html)
        self.assertNotIn("EXCESSIVE:", html)
        ids = [row[0] for row in sqlite3.connect(db_path).execute("SELECT entity_id FROM entities")]
        self.assertEqual(len(ids), 8)
        for entity in ids:
            self.assertRegex(entity, FAKE)
        for found in UUID.findall(html):
            self.assertRegex(found, FAKE)

    def test_one_tool_near_weekly_limit_and_none_exceed_it(self):
        cfg = budgets.load_budgets(budgets.DEFAULT_BUDGETS_PATH)
        limit = float(cfg["per_tool"]["weekly_usd"])
        for seed in (1, 2, 7, 99):
            html, db_path = self.render(seed)
            db = sqlite3.connect(db_path)
            rows = budgets.rows_from_db(db)
            db.close()
            self.assertEqual(budgets.find_breaches(rows, cfg, TODAY), [])
            self.assertNotIn("EXCEEDED", html)
            start, end, _prev_s, _prev_e = budgets.week_bounds(TODAY)
            by_tool = {}
            for row in rows:
                if start <= row["day"] <= end:
                    by_tool[row["tool"]] = by_tool.get(row["tool"], 0.0) + float(row["cost"])
            near = [name for name, spent in by_tool.items() if 0.89 * limit <= spent <= 0.91 * limit]
            self.assertEqual(near, ["claude"])
            for name, spent in by_tool.items():
                self.assertLess(spent, limit)
                self.assertGreater(spent, 0)

    def test_cli_writes_default_names(self):
        self.assertEqual(demo.default_out_path().name, "dashboard.html")
        self.assertEqual(demo.default_out_path().parent.name, "demo")
        self.assertEqual(demo.default_db_path().suffix, ".db")
        out = Path(self.tmp.name) / "out" / "dashboard.html"
        db = Path(self.tmp.name) / "demo.db"
        code = demo.main(["--db", str(db), "--out", str(out), "--seed", "4"])
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("Atlas", text)
        self.assertIn('lang="en"', text)
        self.assertTrue(db.exists())

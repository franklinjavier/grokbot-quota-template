"""Weekly CLI spend, bot score change, sparklines, and freshness."""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quota import collect, report, trends  # noqa: E402

TZ = ZoneInfo("Europe/Lisbon")
TODAY = date(2026, 10, 15)


def noon(day: date) -> str:
    dt = datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


class TrendMathTest(unittest.TestCase):
    def test_cli_spend_this_week_vs_previous(self):
        rows = [
            {"tool": "codex", "day": TODAY, "cost": 4.0, "estimated": True},
            {"tool": "codex", "day": TODAY - timedelta(days=20), "cost": 99, "estimated": True},
            {"tool": "claude", "day": date(2026, 10, 5), "cost": 2.5, "estimated": False},
            {"tool": "grok", "day": date(2026, 1, 1), "cost": 50, "estimated": False},
        ]
        got = trends.weekly_cli_spend(rows, TODAY)
        by = {row["tool"]: row for row in got}
        self.assertEqual(set(by), {"claude", "codex"})
        self.assertAlmostEqual(by["codex"]["this_usd"], 4.0)
        self.assertAlmostEqual(by["codex"]["prev_usd"], 0.0)
        self.assertTrue(by["codex"]["this_estimated"])
        self.assertFalse(by["codex"]["prev_estimated"])
        self.assertAlmostEqual(by["claude"]["prev_usd"], 2.5)
        self.assertFalse(by["claude"]["prev_estimated"])
        self.assertEqual([row["tool"] for row in got], ["claude", "codex"])

    def test_bot_scores_change_and_sparkline(self):
        this_start = TODAY - timedelta(days=6)
        prev_day = this_start - timedelta(days=1)
        rows = [
            {"entity_id": "beta", "day": TODAY, "score": 3},
            {"entity_id": "beta", "day": prev_day, "score": 2},
            {"entity_id": "alpha", "day": TODAY.isoformat(), "score": 9},
            {"entity_id": "alpha", "day": this_start, "score": 1},
            {"entity_id": "quiet", "day": date(2026, 1, 1), "score": 100},
        ]
        got = trends.bot_score_trends(rows, TODAY)
        self.assertEqual([row["entity_id"] for row in got], ["alpha", "beta"])
        alpha, beta = got
        self.assertEqual(alpha["this_score"], 10)
        self.assertEqual(alpha["prev_score"], 0)
        self.assertIsNone(alpha["change_pct"])
        self.assertIsNone(alpha["change_label"])
        self.assertEqual(len(alpha["daily"]), 7)
        self.assertEqual(alpha["days"][0], this_start.isoformat())
        self.assertEqual(alpha["days"][-1], TODAY.isoformat())
        self.assertEqual(alpha["daily"][0], 1)
        self.assertEqual(alpha["daily"][-1], 9)
        self.assertEqual(sum(alpha["daily"]), 10)
        self.assertEqual(beta["this_score"], 3)
        self.assertEqual(beta["prev_score"], 2)
        self.assertAlmostEqual(beta["change_pct"], 50)
        self.assertEqual(beta["change_label"], "+50%")
        self.assertEqual(trends.format_change(-12.4), "-12%")
        self.assertEqual(trends.format_change(0), "0%")
        self.assertIsNone(trends.format_change(None))
        self.assertIsNone(trends.change_pct(4, 0))
        self.assertEqual(trends.change_pct(0, 0), 0.0)

    def test_freshness_passes_through_ms(self):
        info = trends.freshness_info(50, None)
        self.assertEqual(info, {"last_collect_ms": 50, "max_search_index_message_ms": None})
        self.assertEqual(trends.freshness_info(None, 0)["max_search_index_message_ms"], 0)


class TrendLoaderTest(unittest.TestCase):
    def test_loader_reads_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = collect.open_db(os.path.join(tmp.name, "q.db"))
        this_start = TODAY - timedelta(days=6)
        prev_day = this_start - timedelta(days=1)
        db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, ts, cost_usd, cost_usd_est, cost_estimated) "
            "VALUES ('s','codex:1','codex','unattributed', ?, NULL, 4, 1)", (noon(TODAY),))
        db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, ts, cost_usd) "
            "VALUES ('s','claude:1','claude','bot-a', ?, 2.5)", (noon(date(2026, 10, 5)),))
        db.execute("INSERT INTO cursors (source_id, generation) VALUES ('agent-transcripts/a/a.jsonl', 0)")
        db.execute("INSERT INTO cursors (source_id, generation) VALUES ('search-index.db#messages', 0)")
        db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, "
            "entity_id, root_entity_id, est_day) VALUES "
            "('agent-transcripts/a/a.jsonl', 0, 'today', 'inference_step', 'exact', 'bot-a', 'bot-a', ?)",
            (TODAY.isoformat(),))
        db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, "
            "entity_id, root_entity_id, est_day) VALUES "
            "('agent-transcripts/a/a.jsonl', 0, 'prev-1', 'inference_step', 'exact', 'bot-a', 'bot-a', ?)",
            (prev_day.isoformat(),))
        db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, "
            "entity_id, root_entity_id, est_day) VALUES "
            "('agent-transcripts/a/a.jsonl', 0, 'prev-2', 'inference_step', 'exact', 'bot-a', 'bot-a', ?)",
            (prev_day.isoformat(),))
        db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, occurred_at_ms) "
            "VALUES ('search-index.db#messages', 0, 'a/1', 'message', 'exact', 100)")
        db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, occurred_at_ms) "
            "VALUES ('search-index.db#messages', 0, 'a/2', 'media', 'exact', 999)")
        db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, occurred_at_ms) "
            "VALUES ('search-index.db#messages', 1, 'a/3', 'message', 'exact', 5000)")
        db.execute("INSERT INTO collector_runs (started_at_ms, ended_at_ms) VALUES (10, 50)")
        db.execute("INSERT INTO collector_runs (started_at_ms, ended_at_ms) VALUES (20, 40)")
        db.commit()
        got = report.dashboard_trends(db, TODAY)
        db.close()
        self.assertEqual(got["this_week"], ["2026-10-09", "2026-10-15"])
        self.assertEqual(got["prev_week"], ["2026-10-02", "2026-10-08"])
        cli = {row["tool"]: row for row in got["cli_by_tool"]}
        self.assertAlmostEqual(cli["codex"]["this_usd"], 4)
        self.assertTrue(cli["codex"]["this_estimated"])
        self.assertAlmostEqual(cli["claude"]["prev_usd"], 2.5)
        self.assertFalse(cli["claude"]["prev_estimated"])
        self.assertEqual(len(got["bots"]), 1)
        bot = got["bots"][0]
        self.assertEqual(bot["entity_id"], "bot-a")
        self.assertAlmostEqual(bot["this_score"], 1.0)
        self.assertAlmostEqual(bot["prev_score"], 2.0)
        self.assertEqual(bot["change_label"], "-50%")
        self.assertEqual(len(bot["daily"]), 7)
        self.assertAlmostEqual(bot["daily"][-1], 1.0)
        self.assertEqual(got["freshness"], {
            "last_collect_ms": 50,
            "max_search_index_message_ms": 100,
        })


if __name__ == "__main__":
    unittest.main()

"""Alert rules: budget near/over, unattributed share, and stale data."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quota.alerts import build_alerts, classify_spend  # noqa: E402


def _budget(kind, name, window, spend, limit, estimated=False, routine=None, label=None):
    return {
        "kind": kind,
        "name": name,
        "label": label or name,
        "window": window,
        "spend": spend,
        "limit": limit,
        "estimated": estimated,
        "routine": routine,
    }


class AlertRulesTest(unittest.TestCase):
    def test_budget_near_over_and_actions(self):
        self.assertEqual(classify_spend(59.99, 75), None)
        self.assertEqual(classify_spend(60, 75), "near")
        self.assertEqual(classify_spend(75, 75), "near")
        self.assertEqual(classify_spend(75.01, 75), "over")
        self.assertIsNone(classify_spend(10, None))

        found = build_alerts({"budgets": [
            _budget("tool", "grok", "daily", 15.99, 20),
            _budget("tool", "claude", "weekly", 60, 75),
            _budget("tool", "codex", "daily", 40, 20, estimated=True),
            _budget("bot", "atlas", "weekly", 80, 75, routine="Weekday digest", label="Atlas"),
            _budget("bot", "quiet", "daily", 4, 20, routine=None, label="Quiet"),
        ]})
        texts = [item["text"] for item in found]
        self.assertEqual([item["kind"] for item in found], ["budget", "budget", "budget"])
        self.assertEqual([item["level"] for item in found], ["over", "over", "near"])
        self.assertEqual([item["severity"] for item in found], ["high", "high", "medium"])
        self.assertIn("codex daily spend is $40.00 (estimated) of $20.00 (200%)", texts[0])
        self.assertIn("switch tool or adjust the budget", texts[0])
        self.assertIn("pause or adjust routine Weekday digest", texts[1])
        self.assertIn("Atlas weekly spend is $80.00 of $75.00 (107%)", texts[1])
        self.assertIn("claude weekly spend is $60.00 of $75.00 (80%)", texts[2])
        self.assertNotIn("grok", " ".join(texts))
        self.assertNotIn("Quiet", " ".join(texts))
        for item in found:
            self.assertIn(" — ", item["text"])
            self.assertTrue(item["text"].endswith(item["action"]))

        bare = build_alerts({"budgets": [
            _budget("bot", "solo", "weekly", 90, 75, label="Solo"),
        ]})
        self.assertEqual(bare[0]["action"], "adjust the budget")
        self.assertEqual(bare[0]["level"], "over")

    def test_unattributed_share_threshold(self):
        self.assertEqual(build_alerts({"unattributed_share": 0.20}), [])
        self.assertEqual(build_alerts({"unattributed_share": 0}), [])
        self.assertEqual(build_alerts({"unattributed_share": None}), [])
        found = build_alerts({"unattributed_share": 0.201})
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual(item["kind"], "unattributed")
        self.assertEqual(item["severity"], "medium")
        self.assertEqual(item["action"], "adopt grokbot-run")
        self.assertIn("20.1%", item["problem"])
        self.assertEqual(item["text"], item["problem"] + " — adopt grokbot-run")
        even = build_alerts({"unattributed_share": 0.36})
        self.assertIn("36%", even[0]["problem"])
        self.assertNotIn("36.0%", even[0]["problem"])

    def test_stale_data_suggests_collector_check(self):
        one = build_alerts({"stale_parts": ["last collect 31 h ago (14/10 08:48)"]})
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0]["severity"], "high")
        self.assertEqual(one[0]["kind"], "stale")
        self.assertEqual(one[0]["action"], "check the routine/collector")
        self.assertEqual(
            one[0]["problem"],
            "Stale data: last collect 31 h ago (14/10 08:48)")
        self.assertTrue(one[0]["text"].endswith(" — check the routine/collector"))

        both = build_alerts({"stale_parts": [
            "last collect 31 h ago (14/10 08:48)",
            "last search-index message 40 h ago (13/10 23:48)",
        ]})
        self.assertEqual(len(both), 1)
        self.assertIn(
            "Stale data: last collect 31 h ago (14/10 08:48); "
            "last search-index message 40 h ago (13/10 23:48)",
            both[0]["text"])

        self.assertEqual(build_alerts({"stale_parts": []}), [])
        self.assertEqual(build_alerts({"stale_parts": [None, ""]}), [])

    def test_severity_order_and_cap(self):
        budgets = [
            _budget("tool", f"t{i}", "daily", 20 + i, 20) for i in range(8)
        ]
        found = build_alerts({
            "budgets": budgets,
            "unattributed_share": 0.5,
            "stale_parts": ["last collect 31 h ago (14/10 08:48)"],
        })
        self.assertEqual(len(found), 6)
        self.assertTrue(all(item["kind"] == "budget" and item["level"] == "over" for item in found))
        spends = [item["problem"] for item in found]
        self.assertIn("$27.00", spends[0])
        self.assertIn("$22.00", spends[-1])
        self.assertNotIn("$21.00", " ".join(spends))
        self.assertNotIn("adopt grokbot-run", " ".join(item["text"] for item in found))
        self.assertNotIn("Stale data", " ".join(item["text"] for item in found))

        mixed = build_alerts({
            "budgets": [_budget("tool", "claude", "weekly", 60, 75)],
            "unattributed_share": 0.33,
            "stale_parts": ["last collect 30 h ago (14/10 09:00)"],
        })
        self.assertEqual([item["kind"] for item in mixed], ["stale", "budget", "unattributed"])
        self.assertEqual([item["severity"] for item in mixed], ["high", "medium", "medium"])


if __name__ == "__main__":
    unittest.main()

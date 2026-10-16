"""Codex cost estimates, price-table config, and Lisbon budget lines."""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quota import budgets, collect, prices  # noqa: E402
from quota.rows import bind as bind_rows  # noqa: E402

TZ = ZoneInfo("Europe/Lisbon")


def _restore_env(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def write_rollout(codex, name, session, usage, *, session_model=None, turns=(), after=(), cwd="/work/x"):
    folder = os.path.join(codex, "2026", "10", "10")
    os.makedirs(folder, exist_ok=True)
    payload = {"session_id": session, "cwd": cwd}
    if session_model is not None:
        payload["model"] = session_model
    lines = [{
        "timestamp": "2026-10-10T00:00:00Z", "type": "session_meta", "payload": payload,
    }]
    for i, model in enumerate(turns):
        lines.append({
            "timestamp": f"2026-10-10T00:00:{i + 1:02d}Z", "type": "turn_context",
            "payload": {"model": model},
        })
    lines.append({
        "timestamp": "2026-10-10T00:01:00Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": usage}},
    })
    for i, model in enumerate(after):
        lines.append({
            "timestamp": f"2026-10-10T00:02:{i:02d}Z", "type": "turn_context",
            "payload": {"model": model},
        })
    path = os.path.join(folder, f"rollout-{name}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


class PriceMathTest(unittest.TestCase):
    def setUp(self):
        self.prices = prices.load_prices(prices.DEFAULT_PRICES_PATH)

    def test_repo_table_and_effective_cost(self):
        self.assertEqual(self.prices["default_model"], "gpt-6.1-sol")
        self.assertEqual(self.prices["models"]["gpt-6.1-sol"]["output"], 10)
        self.assertEqual(self.prices["_source"].startswith("https://developers.openai.com/"), True)
        self.assertEqual(prices.effective_cost(1.5, 9), 1.5)
        self.assertEqual(prices.effective_cost(0, 9), 0)
        self.assertEqual(prices.effective_cost(None, 2.25), 2.25)
        self.assertIsNone(prices.effective_cost(None, None))

    def test_cached_subtraction_output_and_ignored_reasoning(self):
        # 1M input includes 400k cached. Uncached 600k * $2 + cached 400k * $0.10.
        usd, defaulted = prices.estimate_codex_cost(
            self.prices, "gpt-6.1-sol",
            input_tokens=1_000_000, cached_input_tokens=400_000,
            cache_write_tokens=0, output_tokens=0, reasoning_tokens=9_000_000)
        self.assertFalse(defaulted)
        self.assertAlmostEqual(usd, 1.24)
        # Forgetting to subtract the cache would charge 2.04.
        self.assertNotAlmostEqual(usd, 2.04)
        usd2, _ = prices.estimate_codex_cost(
            self.prices, "gpt-6.1-sol",
            input_tokens=1_000_000, cached_input_tokens=400_000,
            output_tokens=0, reasoning_tokens=0)
        self.assertAlmostEqual(usd, usd2)
        write, _ = prices.estimate_codex_cost(
            self.prices, "gpt-6-astra",
            cache_write_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(write, 62.50)

    def test_unknown_model_uses_default(self):
        known, known_flag = prices.estimate_codex_cost(
            self.prices, "gpt-6-luna", input_tokens=1_000_000)
        self.assertFalse(known_flag)
        self.assertAlmostEqual(known, 0.10)
        unknown, flag = prices.estimate_codex_cost(
            self.prices, "not-a-model", input_tokens=1_000_000, cached_input_tokens=0,
            output_tokens=0)
        missing, missing_flag = prices.estimate_codex_cost(
            self.prices, None, input_tokens=1_000_000)
        self.assertTrue(flag)
        self.assertTrue(missing_flag)
        self.assertAlmostEqual(unknown, 2.00)  # default gpt-6.1-sol input
        self.assertAlmostEqual(unknown, missing)

    def test_prices_path_override(self):
        self.assertEqual(prices.resolve_prices_path("/explicit.json"), "/explicit.json")
        os.environ["QUOTA_PRICES"] = "/from-env.json"
        try:
            self.assertEqual(prices.resolve_prices_path(None), "/from-env.json")
            self.assertEqual(prices.resolve_prices_path("/explicit.json"), "/explicit.json")
        finally:
            del os.environ["QUOTA_PRICES"]
        self.assertEqual(prices.resolve_prices_path(None), prices.DEFAULT_PRICES_PATH)

    def test_reported_cost_is_not_replaced(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = collect.open_db(os.path.join(tmp.name, "q.db"))
        db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, model, cost_usd, "
            "input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, reasoning_tokens) "
            "VALUES ('s','codex:keep','codex','unattributed','gpt-6.1-sol', 3.5, "
            "1000000, 1000000, 0, 0, 5000000)")
        db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, model, "
            "input_tokens, cache_read_tokens, cache_create_tokens, output_tokens, reasoning_tokens) "
            "VALUES ('s','codex:est','codex','unattributed','nope', 1000000, 0, 0, 0, 5000000)")
        db.commit()
        collect.refresh_codex_estimates(db, prices.DEFAULT_PRICES_PATH)
        kept = db.execute("SELECT cost_usd, cost_usd_est, cost_estimated, model_defaulted "
                          "FROM cli_runs WHERE run_key='codex:keep'").fetchone()
        self.assertEqual(kept["cost_usd"], 3.5)
        self.assertIsNone(kept["cost_usd_est"])
        self.assertEqual(kept["cost_estimated"], 0)
        est = db.execute("SELECT cost_usd, cost_usd_est, cost_estimated, model_defaulted, model "
                         "FROM cli_runs WHERE run_key='codex:est'").fetchone()
        self.assertIsNone(est["cost_usd"])
        self.assertAlmostEqual(est["cost_usd_est"], 2.0)
        self.assertEqual(est["cost_estimated"], 1)
        self.assertEqual(est["model_defaulted"], 1)
        self.assertEqual(est["model"], "nope")
        db.close()


class CodexModelCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "root")
        self.codex = os.path.join(self.tmp.name, "codex")
        self.db_path = os.path.join(self.tmp.name, "quota.db")
        os.makedirs(self.root)
        os.makedirs(self.codex)
        usage = {
            "input_tokens": 1_000_000, "cached_input_tokens": 200_000,
            "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 50_000,
        }
        # A prompt is ignored. The priced model arrives after the token total.
        write_rollout(self.codex, "after", "sess-after", usage,
                      turns=("ignore this CANARY prompt",), after=("gpt-6-luna",))
        write_rollout(self.codex, "unknown", "sess-unknown", {
            "input_tokens": 1_000_000, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 1,
        }, session_model="not-a-priced-model")

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            collect.main([
                "--db", self.db_path, "--root", self.root,
                "--panel", os.path.join(self.tmp.name, "panel.html"),
                "--codex-sessions", self.codex,
                "--grok-sessions", os.path.join(self.tmp.name, "grok"),
                "--launches", os.path.join(self.tmp.name, "launches.jsonl"),
                "--backfill", *extra,
            ])
        return out.getvalue()

    def test_turn_context_model_and_unknown_default(self):
        self.collect()
        con = sqlite3.connect(self.db_path)
        bind_rows(con)
        luna = con.execute("SELECT * FROM cli_runs WHERE session_id='sess-after'").fetchone()
        # (1e6-2e5)*0.10 + 2e5*0.01 = 0.082. Reasoning is not added on top.
        self.assertEqual(luna["model"], "gpt-6-luna")
        self.assertEqual(luna["model_defaulted"], 0)
        self.assertEqual(luna["cost_estimated"], 1)
        self.assertIsNone(luna["cost_usd"])
        self.assertAlmostEqual(luna["cost_usd_est"], 0.082)
        unknown = con.execute("SELECT * FROM cli_runs WHERE session_id='sess-unknown'").fetchone()
        self.assertEqual(unknown["model"], "not-a-priced-model")
        self.assertEqual(unknown["model_defaulted"], 1)
        self.assertAlmostEqual(unknown["cost_usd_est"], 2.0)
        blob = " ".join((row[0] or "") for row in con.execute("SELECT raw_json FROM cli_runs"))
        self.assertNotIn("CANARY", blob)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM cli_runs WHERE tool='codex'").fetchone()[0], 2)
        con.close()
        self.collect()
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM cli_runs WHERE tool='codex'").fetchone()[0], 2)
        con.close()

    def test_prices_flag_overrides_env(self):
        zero = os.path.join(self.tmp.name, "zero.json")
        with open(zero, "w", encoding="utf-8") as f:
            json.dump({
                "models": {"gpt-6-luna": {"input": 0, "cached_input": 0, "cache_write": 0, "output": 0},
                           "gpt-6.1-sol": {"input": 0, "cached_input": 0, "cache_write": 0, "output": 0}},
                "default_model": "gpt-6.1-sol",
            }, f)
        pricey = os.path.join(self.tmp.name, "pricey.json")
        with open(pricey, "w", encoding="utf-8") as f:
            json.dump({
                "models": {"gpt-6-luna": {"input": 100, "cached_input": 100, "cache_write": 100, "output": 100},
                           "gpt-6.1-sol": {"input": 100, "cached_input": 100, "cache_write": 100, "output": 100}},
                "default_model": "gpt-6.1-sol",
            }, f)
        os.environ["QUOTA_PRICES"] = pricey
        try:
            self.collect("--prices", zero)
        finally:
            os.environ.pop("QUOTA_PRICES", None)
        con = sqlite3.connect(self.db_path)
        cost = con.execute(
            "SELECT cost_usd_est FROM cli_runs WHERE session_id='sess-after'").fetchone()[0]
        con.close()
        self.assertEqual(cost, 0)


class BudgetLinesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = budgets.load_budgets(budgets.DEFAULT_BUDGETS_PATH)
        self.today = date(2026, 10, 15)

    def test_repo_defaults(self):
        self.assertEqual(self.cfg["per_tool"], {"daily_usd": 20, "weekly_usd": 75})
        self.assertEqual(self.cfg["per_bot"], {"daily_usd": 20, "weekly_usd": 75})
        self.assertEqual(self.cfg["overrides"], {"tools": {}, "bots": {}})
        self.assertIn("Europe/Lisbon", self.cfg["week"])

    def test_lisbon_day_boundary(self):
        # October 2026 is still WEST (UTC+1): 23:30Z is the next Lisbon morning.
        self.assertEqual(budgets.lisbon_day_of("2026-10-14T23:30:00Z"), date(2026, 10, 15))
        self.assertEqual(budgets.lisbon_day_of("2026-10-15T23:30:00Z"), date(2026, 10, 16))

    def lines(self, rows, cfg=None, today=None):
        return budgets.format_lines(budgets.find_breaches(rows, cfg or self.cfg, today or self.today))

    def test_excessive_and_ok(self):
        rows = [{"tool": "grok", "bot_id": "beta", "day": self.today, "cost": 82.10, "estimated": False}]
        self.assertEqual(self.lines(rows), [
            "EXCESSIVE: tool grok daily $82.10 > $20.00 (2026-10-15..2026-10-15)",
            "EXCESSIVE: tool grok weekly $82.10 > $75.00 (2026-10-09..2026-10-15)",
            "EXCESSIVE: bot beta daily $82.10 > $20.00 (2026-10-15..2026-10-15)",
            "EXCESSIVE: bot beta weekly $82.10 > $75.00 (2026-10-09..2026-10-15)",
        ])
        estimated = [{"tool": "codex", "bot_id": "beta", "day": self.today, "cost": 82.10, "estimated": True}]
        text = "\n".join(self.lines(estimated))
        self.assertIn("EXCESSIVE: tool codex weekly $82.10 > $75.00 (2026-10-09..2026-10-15) (estimated)", text)
        self.assertIn("EXCESSIVE: bot beta daily $82.10 > $20.00 (2026-10-15..2026-10-15) (estimated)", text)
        # At the limit, and the day before the window, are not breaches.
        self.assertEqual(self.lines([
            {"tool": "grok", "bot_id": "beta", "day": self.today, "cost": 20, "estimated": False},
        ]), ["budgets: ok"])
        self.assertEqual(self.lines([
            {"tool": "grok", "bot_id": "beta", "day": self.today - timedelta(days=7), "cost": 100, "estimated": False},
        ]), ["budgets: ok"])
        # Weekly only: the spend is inside the 7 days but not today.
        weekly = self.lines([
            {"tool": "grok", "bot_id": "beta", "day": self.today - timedelta(days=6),
             "cost": 80, "estimated": False},
        ])
        self.assertEqual(weekly, [
            "EXCESSIVE: tool grok weekly $80.00 > $75.00 (2026-10-09..2026-10-15)",
            "EXCESSIVE: bot beta weekly $80.00 > $75.00 (2026-10-09..2026-10-15)",
        ])

    def test_unattributed_is_tool_only_and_override(self):
        rows = [{"tool": "grok", "bot_id": "unattributed", "day": self.today, "cost": 82.10, "estimated": False}]
        lines = self.lines(rows)
        self.assertTrue(any(line.startswith("EXCESSIVE: tool grok ") for line in lines))
        self.assertFalse(any("bot " in line for line in lines))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["overrides"]["tools"]["grok"] = {"weekly_usd": 10}
        cfg["overrides"]["bots"]["beta"] = {"daily_usd": 100, "weekly_usd": 100}
        lines = self.lines([
            {"tool": "grok", "bot_id": "beta", "day": self.today, "cost": 12, "estimated": False},
        ], cfg)
        self.assertEqual(lines, [
            "EXCESSIVE: tool grok weekly $12.00 > $10.00 (2026-10-09..2026-10-15)",
        ])

    def test_collect_report_prints_excessive(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "root")
        os.makedirs(root)
        db_path = os.path.join(tmp.name, "quota.db")
        codex = os.path.join(tmp.name, "codex")
        grok = os.path.join(tmp.name, "grok")
        os.makedirs(codex)
        os.makedirs(grok)
        tasks = os.path.join(root, "tasks.db")
        con = sqlite3.connect(tasks)
        con.execute("CREATE TABLE projects (id TEXT, name TEXT, bot_id TEXT)")
        con.execute("CREATE TABLE tasks (id TEXT, project_id TEXT)")
        con.execute("INSERT INTO projects VALUES ('project-demo', 'Project Demo', 'beta')")
        con.commit()
        con.close()
        saved = {key: os.environ.get(key) for key in ("QUOTA_ATTRIBUTION_PLUGIN", "QUOTA_TASKS_DB")}
        os.environ["QUOTA_ATTRIBUTION_PLUGIN"] = "quota.plugins.sqlite_tasks:lookup"
        os.environ["QUOTA_TASKS_DB"] = tasks
        self.addCleanup(_restore_env, saved)
        base = ["--db", db_path, "--root", root, "--panel", os.path.join(tmp.name, "panel.html"),
                "--codex-sessions", codex, "--grok-sessions", grok,
                "--launches", os.path.join(tmp.name, "launches.jsonl")]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            collect.main(base)
        today = budgets.lisbon_today()
        noon = datetime(today.year, today.month, today.day, 12, 0, tzinfo=TZ)
        ts = noon.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, cwd, ts, model, cost_usd, "
            "input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, reasoning_tokens) "
            "VALUES ('manual-codex','codex:budget','codex','unattributed', NULL, ?, NULL, NULL, "
            "0, 8210000, 0, 0, 999999)", (ts,))
        con.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, cwd, ts, cost_usd) "
            "VALUES ('manual-grok','grok:budget','grok','unattributed','/work/project-demo', ?, 82.10)", (ts,))
        con.commit()
        con.close()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            collect.main([*base, "--report"])
        start = today - timedelta(days=6)
        lines = [line for line in out.getvalue().splitlines() if line.startswith("EXCESSIVE:")]
        self.assertEqual(lines, [
            f"EXCESSIVE: tool codex daily $82.10 > $20.00 ({today}..{today}) (estimated)",
            f"EXCESSIVE: tool codex weekly $82.10 > $75.00 ({start}..{today}) (estimated)",
            f"EXCESSIVE: tool grok daily $82.10 > $20.00 ({today}..{today})",
            f"EXCESSIVE: tool grok weekly $82.10 > $75.00 ({start}..{today})",
            f"EXCESSIVE: bot beta daily $82.10 > $20.00 ({today}..{today})",
            f"EXCESSIVE: bot beta weekly $82.10 > $75.00 ({start}..{today})",
        ])
        con = sqlite3.connect(db_path)
        bind_rows(con)
        codex_row = con.execute("SELECT * FROM cli_runs WHERE run_key='codex:budget'").fetchone()
        grok_row = con.execute("SELECT * FROM cli_runs WHERE run_key='grok:budget'").fetchone()
        con.close()
        self.assertIsNone(codex_row["cost_usd"])
        self.assertAlmostEqual(codex_row["cost_usd_est"], 82.10)
        self.assertEqual(codex_row["cost_estimated"], 1)
        self.assertEqual(codex_row["model_defaulted"], 1)
        self.assertEqual(codex_row["bot_id"], "unattributed")
        self.assertEqual(grok_row["cost_usd"], 82.10)
        self.assertEqual(grok_row["bot_id"], "beta")
        self.assertIsNone(grok_row["cost_usd_est"])


if __name__ == "__main__":
    unittest.main()

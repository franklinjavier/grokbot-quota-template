"""Dashboard ranking lists: every active bot, overflow in <details>."""
import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quota import budgets, collect, dashboard  # noqa: E402

LISBON = ZoneInfo("Europe/Lisbon")

START, END = date(2026, 9, 20), date(2026, 9, 30)
DAY = "2026-09-25"
MS = int(datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def uid(n):
    # Fake id. See README: 00000000-0000-4000-8000- plus 0000000000 and two hex digits.
    return f"00000000-0000-4000-8000-{n:012x}"


class DashboardBotListTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agents = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.agents)
        self.db = collect.open_db(os.path.join(self.tmp.name, "quota.db"))
        self.db.execute(
            "INSERT INTO cursors (source_id, generation, byte_offset, last_seq, backlog_bytes) "
            "VALUES ('t', 0, 0, 0, 0)")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add_bot(self, n, name, steps, *, store_name=True):
        eid = uid(n)
        folder = os.path.join(self.agents, eid)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "profile.json"), "w", encoding="utf-8") as f:
            json.dump({"name": name}, f)
        self.db.execute(
            "INSERT INTO entities (entity_id, kind, display_name, profile_present) VALUES (?,?,?,?)",
            (eid, "bot", name if store_name else None, int(bool(store_name and name))))
        for i in range(steps):
            self.db.execute(
                "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, "
                "entity_id, est_at_ms, est_day, root_entity_id) VALUES (?,?,?,?,?,?,?,?,?)",
                ("t", 0, f"{eid}:{i}", "inference_step", "exact", eid, MS, DAY, eid))
        return eid

    def html(self):
        return dashboard.build_html(self.db, START, END, agents=self.agents)

    def test_more_than_ten_bots_overflow_in_details(self):
        names = [f"Bot {i:02d}" for i in range(11)]
        for i, name in enumerate(names):
            self.add_bot(i, name, steps=12 - i)
        quiet = self.add_bot(11, "Quiet Bot", steps=1, store_name=False)
        page = dashboard.build_html(self.db, START, END, agents=self.agents, now=WHEN)
        head, sep, tail = page.partition("<details>")
        self.assertTrue(sep, "overflow bots must live in a <details> block")
        self.assertEqual(head.count('<tr class="bot">'), 10)
        self.assertIn(f"<summary>others ({fig(2)})</summary>", page)
        first = tail.split("</details>", 1)[0]
        self.assertEqual(first.count('<tr class="bot">'), 2)
        self.assertEqual(page.count(f"<summary>others ({fig(2)})</summary>"), 2)
        for name in names[:10]:
            self.assertIn(f"<strong>{name}</strong>", head)
            self.assertNotIn(f"<strong>{name}</strong>", first)
        self.assertIn("<strong>Bot 10</strong>", first)
        self.assertIn("<strong>Quiet Bot</strong>", first)
        self.assertNotIn("Quiet Bot", head)
        self.assertNotIn(quiet, page)
        self.assertNotIn("unnamed", page)

    def test_plan_usage_stack_and_rank(self):
        self.add_bot(0, "Alpha", steps=3)
        self.add_bot(1, "Beta", steps=1)
        self.db.execute("INSERT INTO usage_readings VALUES (?,?,?,?)", (DAY, 40, "private note", MS))
        page = self.html()
        hero = page.split('<section class="hero">', 1)[1].split('</section>', 1)[0]
        self.assertIn('40%</span> of plan used', hero)
        self.assertIn('width:30.000%', hero)
        self.assertIn('width:10.000%', hero)
        self.assertIn('free 60%', hero)
        self.assertIn('<th class="num">% of plan</th>', hero)
        self.assertIn('30.0%', bot_row(hero, "Alpha"))
        self.assertIn(f'{fig(3)} steps', bot_row(hero, "Alpha"))
        self.assertNotIn('0 tools', hero)
        self.assertNotIn('private note', page)
        self.assertEqual(page.count('<table class="bots">'), 1)
        self.db.execute("DELETE FROM usage_readings")
        page = self.html()
        self.assertIn('No Usage reading yet.', page)
        self.assertNotIn('<th class="num">% of plan</th>', page)
        self.db.execute("DROP TABLE usage_readings")
        self.assertIn('No Usage reading yet.', self.html())

    def test_low_activity_bot_appears_by_profile_name(self):
        self.add_bot(0, "Loud Bot", steps=5)
        eid = self.add_bot(1, "Quiet Bot", steps=1, store_name=False)
        page = dashboard.build_html(self.db, START, END, agents=self.agents, now=WHEN)
        self.assertIn("<strong>Quiet Bot</strong>", page)
        self.assertIn("<strong>Loud Bot</strong>", page)
        self.assertNotIn("<summary>others", page)
        self.assertNotIn("unnamed", page)
        self.assertNotIn(eid, page)
        self.assertEqual(page.count('<tr class="bot">'), 2)


def noon(day):
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=LISBON).isoformat()


def ms_of(dt):
    return int(dt.timestamp() * 1000)


NOW = datetime(2026, 10, 15, 15, 48, tzinfo=LISBON)


def fig(token):
    return f'<span class="fig">{token}</span>'


def bot_row(page, name):
    marker = f"<strong>{name}</strong>"
    start = page.index(marker)
    open_at = page.rfind("<tr", 0, start)
    close_at = page.index("</tr>", start)
    return page[open_at:close_at]


WHEN = datetime(2026, 9, 30, 12, tzinfo=LISBON)


class DashboardPartsTest(DashboardBotListTest):
    def steps(self, n, name, days):
        eid = uid(n)
        folder = os.path.join(self.agents, eid)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "profile.json"), "w", encoding="utf-8") as f:
            json.dump({"name": name}, f)
        self.db.execute(
            "INSERT INTO entities (entity_id, kind, display_name, profile_present) VALUES (?,?,?,1)",
            (eid, "bot", name))
        for i, day in enumerate(days):
            self.db.execute(
                "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, "
                "entity_id, est_day, root_entity_id) VALUES (?,?,?,?,?,?,?,?)",
                ("t", 0, f"{eid}:{i}", "inference_step", "exact", eid, day.isoformat(), eid))
        return eid

    def test_fresh_page_has_summary_and_no_banner(self):
        page = self.html()
        self.assertNotIn("Stale data", page)
        self.assertLess(page.index("<h2>Bots</h2>"), page.index("CLI spend this week (last 7 days)"))
        self.assertLess(page.index("<h2>Bots</h2>"), page.index("Last 30 days"))
        self.assertIn("<strong>0</strong> activity points (not a money unit, not a quota)", page)
        self.assertIn("$0.00", page)
        self.assertEqual(page.count("no tool data"), 0)
        self.assertNotIn("no tool data", page)
        self.assertNotIn("Most active", page)
        self.assertNotIn("Day by day", page)
        self.assertNotIn("By day", page)

    def test_stale_collect_uses_lisbon_time(self):
        stale = NOW - timedelta(hours=31)
        fresh = NOW - timedelta(hours=1)
        self.db.execute(
            "INSERT INTO collector_runs (started_at_ms, ended_at_ms) VALUES (?, ?)",
            (ms_of(stale) - 1000, ms_of(stale)))
        self.db.execute(
            "INSERT INTO cursors (source_id, generation, byte_offset, last_seq, backlog_bytes) "
            "VALUES ('search-index.db#messages', 0, 0, 0, 0)")
        self.db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, occurred_at_ms) "
            "VALUES ('search-index.db#messages', 0, 'a/1', 'message', 'exact', ?)",
            (ms_of(fresh),))
        page = dashboard.build_html(self.db, START, END, agents=self.agents, now=NOW)
        self.assertIn("Stale data", page)
        self.assertIn(f"last collect {fig(31)} h ago ({fig('14/10 08:48')})", page)
        self.assertIn("check the routine/collector", page)
        self.assertNotIn("last search-index message", page)
        self.assertLess(page.index("Stale data"), page.index("<main>"))
        self.assertIn(".stale{background:#fde8e6;color:#e5534b", page)
        self.assertEqual(page.count('class="stale"'), 1)

    def test_stale_search_index_only(self):
        stale = NOW - timedelta(hours=31)
        fresh = NOW - timedelta(hours=1)
        self.db.execute(
            "INSERT INTO collector_runs (started_at_ms, ended_at_ms) VALUES (?, ?)",
            (ms_of(fresh) - 1000, ms_of(fresh)))
        self.db.execute(
            "INSERT INTO cursors (source_id, generation, byte_offset, last_seq, backlog_bytes) "
            "VALUES ('search-index.db#messages', 0, 0, 0, 0)")
        self.db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, occurred_at_ms) "
            "VALUES ('search-index.db#messages', 0, 'a/1', 'message', 'exact', ?)",
            (ms_of(stale),))
        page = dashboard.build_html(self.db, START, END, agents=self.agents, now=NOW)
        self.assertIn("Stale data", page)
        self.assertIn(
            f"last search-index message {fig(31)} h ago ({fig('14/10 08:48')})", page)
        self.assertIn("check the routine/collector", page)
        self.assertNotIn("last collect", page)

    def test_both_stale_and_26h_boundary(self):
        collect_at = NOW - timedelta(hours=31)
        message_at = NOW - timedelta(hours=40)
        self.db.execute(
            "INSERT INTO collector_runs (started_at_ms, ended_at_ms) VALUES (?, ?)",
            (ms_of(collect_at), ms_of(collect_at)))
        self.db.execute(
            "INSERT INTO cursors (source_id, generation, byte_offset, last_seq, backlog_bytes) "
            "VALUES ('search-index.db#messages', 0, 0, 0, 0)")
        self.db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, occurred_at_ms) "
            "VALUES ('search-index.db#messages', 0, 'a/1', 'message', 'exact', ?)",
            (ms_of(message_at),))
        page = dashboard.build_html(self.db, START, END, agents=self.agents, now=NOW)
        self.assertIn(f"last collect {fig(31)} h ago ({fig('14/10 08:48')})", page)
        self.assertIn(
            f"last search-index message {fig(40)} h ago ({fig(dashboard._stamp(ms_of(message_at)))})",
            page)
        self.assertEqual(page.count('class="stale"'), 1)

        self.db.execute("DELETE FROM collector_runs")
        self.db.execute("DELETE FROM events")
        edge = NOW - timedelta(hours=26)
        self.db.execute(
            "INSERT INTO collector_runs (started_at_ms, ended_at_ms) VALUES (?, ?)",
            (ms_of(edge), ms_of(edge)))
        self.db.execute(
            "INSERT INTO events (source_id, generation, source_key, event_type, time_basis, occurred_at_ms) "
            "VALUES ('search-index.db#messages', 0, 'a/2', 'message', 'exact', ?)",
            (ms_of(edge),))
        fresh_page = dashboard.build_html(self.db, START, END, agents=self.agents, now=NOW)
        self.assertNotIn("Stale data", fresh_page)

        just = NOW - timedelta(hours=26, seconds=1)
        self.db.execute("UPDATE collector_runs SET ended_at_ms=?", (ms_of(just),))
        self.db.execute("UPDATE events SET occurred_at_ms=? WHERE source_key='a/2'", (ms_of(NOW - timedelta(minutes=5)),))
        late = dashboard.build_html(self.db, START, END, agents=self.agents, now=NOW)
        self.assertIn(f"last collect {fig(26)} h ago", late)
        self.assertNotIn("last search-index message", late)

    def test_week_summary_and_card_trend(self):
        today = date(2026, 10, 15)
        prev = today - timedelta(days=7)
        self.steps(1, "Alpha Trend", [prev, prev, today, today, today])
        self.steps(2, "Beta New", [today, today, today, today])
        self.db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, ts, cost_usd) VALUES (?,?,?,?,?,?)",
            ("s", "c-now", "claude", "unattributed", noon(today), 10))
        self.db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, ts, cost_usd) VALUES (?,?,?,?,?,?)",
            ("s", "c-prev", "claude", "unattributed", noon(prev), 5))
        self.db.execute(
            "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, ts, cost_usd, cost_usd_est, cost_estimated) "
            "VALUES (?,?,?,?,?,NULL,?,1)",
            ("s", "x-now", "codex", "unattributed", noon(today), 4))
        page = dashboard.build_html(
            self.db, today - timedelta(days=13), today, agents=self.agents, now=NOW)
        self.assertLess(page.index("<h2>Bots</h2>"), page.index("CLI spend this week (last 7 days)"))
        self.assertIn(
            'class="tool-name">claude</span><span class="tool-amt"><strong>'
            f'{fig("$10.00")}</strong></span>', page)
        self.assertIn('<span class="fig trend-pct">+100%</span>', page)
        self.assertIn('class="tool-name">codex<span class="est">(estimated)</span></span>', page)
        self.assertIn(
            'class="tool-amt"><strong>' + fig("$4.00") + "</strong>", page)
        self.assertIn(
            'class="tool-name">grok</span><span class="tool-amt"><strong>'
            f'{fig("$0.00")}</strong></span>', page)
        self.assertIn('class="tool-name">Total<span class="est">(includes estimated)</span></span>', page)
        self.assertIn(
            'class="tool-amt"><strong>' + fig("$14.00") + "</strong>", page)
        self.assertIn('<span class="fig trend-pct">+180%</span>', page)
        self.assertIn("<strong>7</strong> activity points (not a money unit, not a quota)", page)
        self.assertIn('<span class="fig trend-pct">+250%</span>', page)
        alpha = bot_row(page, "Alpha Trend")
        beta = bot_row(page, "Beta New")
        self.assertIn('<span class="fig trend-pct">+50%</span>', alpha)
        self.assertIn('class="spark"', alpha)
        self.assertEqual(len(alpha.split('points="', 1)[1].split('"', 1)[0].split()), 7)
        self.assertIn(">new<", beta)
        self.assertIn('class="spark"', beta)
        self.assertNotIn("+50%", beta)

    def test_budgets_mark_ok_and_exceeded(self):
        cfg = budgets.load_budgets(budgets.DEFAULT_BUDGETS_PATH)
        daily = float(cfg["per_tool"]["daily_usd"])
        weekly = float(cfg["per_tool"]["weekly_usd"])
        self.assertGreater(weekly, daily + 1)
        today = date(2026, 10, 15)
        alpha, beta, gamma = uid(1), uid(2), uid(3)
        for n, name, eid in ((1, "High Spend", alpha), (2, "Codex Spend", beta), (3, "Low Spend", gamma)):
            folder = os.path.join(self.agents, eid)
            os.makedirs(folder)
            with open(os.path.join(folder, "profile.json"), "w", encoding="utf-8") as f:
                json.dump({"name": name}, f)
            self.db.execute(
                "INSERT INTO entities (entity_id, kind, display_name, profile_present) VALUES (?,?,?,1)",
                (eid, "bot", name))

        def add(key, tool, bot, cost, estimated=False):
            if estimated:
                self.db.execute(
                    "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, ts, cost_usd, cost_usd_est, cost_estimated) "
                    "VALUES ('s',?,?,?,?,NULL,?,1)",
                    (key, tool, bot, noon(today), cost))
            else:
                self.db.execute(
                    "INSERT INTO cli_runs (source_id, run_key, tool, bot_id, ts, cost_usd) VALUES ('s',?,?,?,?,?)",
                    (key, tool, bot, noon(today), cost))

        add("claude", "claude", alpha, daily + 1)
        add("codex", "codex", beta, weekly + 1, estimated=True)
        add("grok", "grok", gamma, 0.5)
        page = dashboard.build_html(self.db, today - timedelta(days=6), today, agents=self.agents, now=NOW)
        banner, main = page.split("<main>", 1)
        self.assertIn("Claude daily", banner)
        self.assertIn("Codex daily", banner)
        self.assertIn("Codex weekly", banner)
        self.assertIn("(estimated)", banner)
        self.assertIn("High Spend", banner)
        self.assertIn("Codex Spend", banner)
        self.assertIn("switch tool or adjust the budget", banner)
        self.assertIn("adjust the budget", banner)
        self.assertNotIn("Low Spend", banner)
        self.assertNotIn("<h2>Budgets</h2>", page)
        self.assertNotIn("EXCEEDED", page)
        self.assertNotIn("EXCESSIVE:", page)
        self.assertNotIn('class="over">ok', page)
        self.assertIn(f"${daily + 1:.2f}", page)
        self.assertIn(">ok<", bot_row(page, "Low Spend"))
        self.assertIn('class="over">over<', bot_row(page, "High Spend"))
        codex_bot = bot_row(page, "Codex Spend")
        self.assertIn('class="over">over<', codex_bot)
        self.assertIn("(estimated)", codex_bot)
        self.assertIn("Low Spend", main)
        self.assertLess(page.index("<h2>Bots</h2>"), page.index("This week's spend"))

    def test_cli_tables_cap_labels_and_columns(self):
        ts = noon(date(2026, 10, 15))
        for i in range(1, 12):
            self.db.execute(
                "INSERT INTO cli_runs (source_id, run_key, tool, task, bot_id, ts, cost_usd, input_tokens) "
                "VALUES ('s',?,?,?,?,?,?,?)",
                (f"k{i}", "claude", f"job-{i:02d}", f"cli-bot-{i:02d}", ts, i, 10))
        page = dashboard.build_html(self.db, START, END, agents=self.agents, now=NOW)
        self.assertIn("cost (USD)", page)
        self.assertIn(">runs<", page)
        self.assertIn(">task<", page)
        self.assertIn(">tool<", page)
        self.assertNotIn(">cost_usd<", page)
        self.assertNotIn("By day", page)
        self.assertNotIn("By tool", page)
        self.assertNotIn("Day by day", page)
        self.assertIn("@media(max-width:700px)", page)
        self.assertIn("grid-template-columns:1fr", page)

        task = page.split("<summary>Tasks</summary>", 1)[1].split("<summary>Routines</summary>", 1)[0]
        top, _sep, rest = task.partition("<details>")
        self.assertIn(f"<summary>others ({fig(1)})</summary>", rest)
        self.assertIn("job-11", top)
        self.assertIn("job-02", top)
        self.assertNotIn("job-01", top)
        self.assertIn("job-01", rest)
        self.assertEqual(top.count("<tr>"), 11)
        head, _sep, tail = page.partition("<details>")
        self.assertIn("cli-bot-11", head)
        self.assertNotIn("cli-bot-01", head)
        self.assertIn("cli-bot-01", tail.split("</details>", 1)[0])

    def test_each_avatar_is_embedded_once(self):
        def blob(tag, size):
            prefix = b"\x89PNG\r\n\x1a\n" + tag
            if len(prefix) >= size:
                return prefix[:size]
            return prefix + b"\x00" * (size - len(prefix))

        small = blob(b"SMALL!", 64)
        bound = blob(b"BOUND!", 60 * 1024)
        large = blob(b"LARGE!", 60 * 1024 + 1)
        specs = (
            (1, "Photo Small", "blob", small),
            (2, "Photo Bound", "pebble", bound),
            (3, "Photo Large", "gem", large),
        )
        for n, name, shape, data in specs:
            eid = self.add_bot(n, name, steps=3)
            folder = os.path.join(self.agents, eid)
            with open(os.path.join(folder, "profile.json"), "w", encoding="utf-8") as f:
                json.dump({"name": name, "avatarShape": shape, "avatarColor": "blue"}, f)
            with open(os.path.join(folder, "avatar.png"), "wb") as f:
                f.write(data)
        page = self.html()
        small_uri = base64.b64encode(small).decode("ascii")
        bound_uri = base64.b64encode(bound).decode("ascii")
        large_uri = base64.b64encode(large).decode("ascii")
        self.assertEqual(page.count(small_uri), 1)
        self.assertEqual(page.count(bound_uri), 1)
        self.assertEqual(page.count(large_uri), 0)
        self.assertEqual(page.count("data:image/png;base64,"), 2)
        self.assertGreater(page.count("av-00000001"), 1)
        self.assertGreater(page.count("av-00000002"), 1)
        self.assertNotIn("av-00000003", page)
        self.assertNotIn("<img", page)
        self.assertIn(dashboard.SHAPES["gem"], page)
        self.assertIn('<span class="avatar photo av-00000001"', page)
        self.assertLess(len(page.encode("utf-8")), 1_000_000)


if __name__ == "__main__":
    unittest.main()

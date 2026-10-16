#!/usr/bin/env python3
"""Build a fake quota database and the demo dashboard.

Usage:
  python3 -m quota.demo [--db PATH] [--out PATH] [--seed N]

The database is invented. Bot ids use the fixture shape
00000000-0000-4000-8000-0000000000NN. Avatars are SVG shapes drawn from
profile color and shape. No PNG files are written.

The default database lives in a temporary directory (any ``*.db`` in the
tree is gitignored). The default HTML path is ``docs/demo/dashboard.html``.
Activity ends on today (Europe/Lisbon). ``--seed`` fixes the variation.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota import budgets, collect, dashboard

# (number, name, avatar color, avatar shape, trend)
BOTS = (
    (1, "Atlas", "blue", "blob", "rise"),
    (2, "Juniper", "green", "pebble", "fall"),
    (3, "Pixel", "violet", "gem", "steady"),
    (4, "Nova", "yellow", "hex", "spike"),
    (5, "Orbit", "cyan", "shield", "weekday"),
    (6, "Maple", "brown", "house", "late"),
    (7, "Comet", "red", "wedge", "quiet"),
    (8, "Quill", "black", "squircle", "wave"),
)
TOOLS = ("claude", "codex", "grok")
# Share of the tool weekly limit. Claude is near the limit; none exceed it.
# This week's claude rows split in half, and the first half is unattributed
# (about 36% of the week), so the demo raises that alert too.
TOOL_SHARE = {"claude": 90, "codex": 24, "grok": 11}
PREV_SHARE = {"claude": 62, "codex": 135, "grok": 95}  # percent of this week's amount
OLD_SHARE = {"claude": 70, "codex": 80, "grok": 75}  # percent, spread over 14 days
NEAR_TOOL = "claude"
DAY_CAP_PERCENT = 55
HISTORY_DAYS = 28
TASKS = 6


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_db_path() -> Path:
    return Path(tempfile.gettempdir()) / "grokbot-quota-demo" / "quota.db"


def default_out_path() -> Path:
    return repo_root() / "docs" / "demo" / "dashboard.html"


def bot_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012x}"


def _as_date(today) -> date:
    if today is None:
        return budgets.lisbon_today()
    if isinstance(today, datetime):
        return budgets.lisbon_today(today)
    return today


def _noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=budgets.TZ)


def _ms(day: date) -> int:
    return int(_noon(day).timestamp() * 1000)


def _dates_ending(end: date, n: int) -> list[date]:
    start = end - timedelta(days=n - 1)
    return [start + timedelta(days=i) for i in range(n)]


def _cents(usd) -> int:
    return int(round(float(usd) * 100))


def _reset_sqlite(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for extra in ("", "-wal", "-shm"):
        candidate = Path(str(path) + extra)
        if candidate.exists():
            candidate.unlink()


def vary_split(total: int, n: int, cap: int, rng: random.Random) -> list[int]:
    """Split ``total`` cents into ``n`` positive parts, each at most ``cap``."""
    if n <= 0 or total < n or total > n * cap:
        raise RuntimeError(f"cannot split {total} cents across {n} days with cap {cap}")
    base, rem = divmod(total, n)
    parts = [base + (1 if i < rem else 0) for i in range(n)]
    for _ in range(n * 4):
        i = rng.randrange(n)
        j = rng.randrange(n)
        if i == j or parts[i] <= 1 or parts[j] >= cap:
            continue
        move = min(rng.randrange(1, 51), parts[i] - 1, cap - parts[j])
        parts[i] -= move
        parts[j] += move
    if sum(parts) != total or any(part < 1 or part > cap for part in parts):
        raise RuntimeError("spend split drifted")
    return parts


def _target_score(kind: str, index: int, day: date) -> float:
    """Unjittered activity points for one bot on one day. Zero skips the day."""
    t = index / (HISTORY_DAYS - 1)
    if kind == "rise":
        return 90 + 340 * t
    if kind == "fall":
        return 420 - 300 * t
    if kind == "steady":
        return 210 + 15 * math.sin(index / 2.5)
    if kind == "spike":
        return 70 + 480 * max(0.0, 1 - abs(t - 0.42) / 0.28)
    if kind == "weekday":
        return 280 if day.weekday() < 5 else 55
    if kind == "late":
        # Quiet until this week, so the card reads as new.
        return 0 if index < HISTORY_DAYS - 7 else 150 + (index - (HISTORY_DAYS - 7)) * 45
    if kind == "quiet":
        return 35 + (index % 4) * 8
    if kind == "wave":
        return 160 + 120 * math.sin(index / 2.7)
    return 100


def _write_profiles(agents: Path, bots) -> None:
    for number, name, color, shape, _kind in bots:
        folder = agents / bot_id(number)
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"name": name, "avatarColor": color, "avatarShape": shape}
        (folder / "profile.json").write_text(json.dumps(payload), encoding="utf-8")
        png = folder / "avatar.png"
        if png.exists():
            png.unlink()


def _insert_event(db, source_id, key, event_type, day, entity, **fields):
    ms = _ms(day)
    columns = {
        "source_id": source_id,
        "generation": 0,
        "source_key": key,
        "event_index": 0,
        "event_type": event_type,
        "occurred_at_ms": ms,
        "time_basis": "exact",
        "entity_id": entity,
        "root_entity_id": entity,
        "est_at_ms": ms,
        "est_day": day.isoformat(),
        "text_bytes": 0,
        "actions": 0,
        "screenshots": 0,
        "duration_ms": 0,
    }
    columns.update(fields)
    names = list(columns)
    db.execute(
        f"INSERT INTO events ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
        [columns[name] for name in names],
    )


def _add_activity(db, rng, today, bots, sources):
    days = _dates_ending(today, HISTORY_DAYS)
    seq = 0
    for number, _name, _color, _shape, kind in bots:
        entity = bot_id(number)
        source_id = sources[entity]
        for index, day in enumerate(days):
            base = _target_score(kind, index, day)
            if base <= 0:
                continue
            target = base * (0.86 + rng.random() * 0.28)
            steps = 1 + rng.randrange(0, 3)
            tools = rng.randrange(0, 5)
            subs = 1 if target >= 80 and rng.random() < 0.3 else 0
            score = 1.0 + steps + 0.5 * tools + 5.0 * subs
            text_bytes = int(round(max(0.0, target - score) * 4000))
            seq += 1
            _insert_event(
                db, source_id, f"req-{seq}", "request_started", day, entity)
            for step in range(steps):
                seq += 1
                _insert_event(
                    db, source_id, f"step-{seq}", "inference_step", day, entity,
                    text_bytes=text_bytes if step == 0 else 0)
            for _tool in range(tools):
                seq += 1
                _insert_event(
                    db, source_id, f"tool-{seq}", "tool_called", day, entity,
                    tool_name="read_file", tool_category="file")
            if subs:
                seq += 1
                _insert_event(
                    db, source_id, f"sub-{seq}", "tool_called", day, entity,
                    tool_name="subagent", tool_category="subagent")
    # A handful of computer-use sessions, spread across the month.
    for number, days_ago, minutes in ((1, 2, 18), (3, 5, 26), (5, 9, 12), (7, 16, 40), (2, 22, 9)):
        entity = bot_id(number)
        day = today - timedelta(days=days_ago)
        seq += 1
        _insert_event(
            db, sources[entity], f"cu-{seq}", "computer_session", day, entity,
            actions=8 + minutes // 4, screenshots=1 + minutes // 15,
            duration_ms=minutes * 60 * 1000)


def _add_cli(db, rng, today, bot_ids, day_cap):
    weekly_cents = _cents(budgets.load_budgets(budgets.DEFAULT_BUDGETS_PATH)["per_tool"]["weekly_usd"])
    this_dates = _dates_ending(today, 7)
    prev_dates = _dates_ending(today - timedelta(days=7), 7)
    old_dates = _dates_ending(today - timedelta(days=14), HISTORY_DAYS - 14)
    windows = (
        (this_dates, {name: weekly_cents * TOOL_SHARE[name] // 100 for name in TOOLS}, True),
        (prev_dates, {name: weekly_cents * TOOL_SHARE[name] // 100 * PREV_SHARE[name] // 100
                      for name in TOOLS}, False),
        (old_dates, {name: weekly_cents * TOOL_SHARE[name] // 100 * OLD_SHARE[name] // 100
                     for name in TOOLS}, False),
    )
    for dates, totals, mark_unattributed in windows:
        for tool in TOOLS:
            parts = vary_split(totals[tool], len(dates), day_cap, rng)
            tool_i = TOOLS.index(tool)
            for day_i, (day, cents) in enumerate(zip(dates, parts)):
                n_parts = 2 if cents >= 2 else 1
                base, rem = divmod(cents, n_parts)
                chunks = [base + (1 if p < rem else 0) for p in range(n_parts)]
                for part_i, chunk in enumerate(chunks):
                    if chunk <= 0:
                        continue
                    unattributed_chunk = mark_unattributed and tool == NEAR_TOOL and part_i == 0
                    if unattributed_chunk:
                        bot = "unattributed"
                        method = "unattributed"
                    else:
                        bot = bot_ids[(day_i + tool_i * 3 + part_i) % len(bot_ids)]
                        method = ("launch_session", "launch_cwd", "plugin")[(day_i + part_i + tool_i) % 3]
                    task = f"JOB-{(day.toordinal() + tool_i + part_i) % TASKS + 1}"
                    when = datetime(day.year, day.month, day.day, 15, 10 + part_i, tzinfo=budgets.TZ)
                    cost = chunk / 100
                    estimated = tool == "codex"
                    db.execute(
                        "INSERT INTO cli_runs (source_id, run_key, tool, task, bot_id, session_id, ts, "
                        "model, num_turns, duration_ms, is_error, cost_usd, input_tokens, output_tokens, "
                        "cwd, attribution_method, cost_usd_est, cost_estimated, model_defaulted) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            f"cli-{tool}",
                            f"{tool}:{day.isoformat()}:{part_i}:{bot}",
                            tool,
                            task,
                            bot,
                            f"demo-{tool}-{day.strftime('%Y%m%d')}-{part_i}",
                            when.isoformat(),
                            {"claude": "claude-sonnet", "codex": "gpt-6.1-sol", "grok": "grok-3"}[tool],
                            3 + (day_i + part_i) % 6,
                            90_000 + day_i * 5_000,
                            0,
                            None if estimated else cost,
                            4_200 + (day.toordinal() + tool_i * 17) % 6_000,
                            900 + (day.toordinal() + part_i * 13) % 2_500,
                            f"/work/{task}",
                            method,
                            cost if estimated else None,
                            1 if estimated else 0,
                            0,
                        ),
                    )


def _add_routines(db, today, bot_ids):
    specs = (
        (bot_ids[0], "weekday-digest", "Weekday digest", 1, "15 8 * * 1-5",
         (("ok", 1), ("ok", 2), ("failed", 4), ("ok", 6))),
        (bot_ids[1], "nightly-index", "Nightly index", 1, "0 2 * * *",
         (("ok", 1), ("ok", 3), ("ok", 8))),
        (bot_ids[3], "monday-review", "Monday review", 0, "30 9 * * 1",
         (("ok", 10), ("failed", 17))),
    )
    for owner, routine_id, name, enabled, schedule, runs in specs:
        db.execute(
            "INSERT INTO routines (owner_id, routine_id, name, enabled, trigger_type, schedule, timezone) "
            "VALUES (?,?,?,?,?,?,?)",
            (owner, routine_id, name, enabled, "cron", schedule, "Europe/Lisbon"))
        for status, days_ago in runs:
            started = _ms(today - timedelta(days=days_ago))
            db.execute(
                "INSERT INTO runs (owner_id, routine_id, run_id, trigger, started_at_ms, finished_at_ms, status) "
                "VALUES (?,?,?,?,?,?,?)",
                (owner, routine_id, f"{routine_id}-{days_ago}", "schedule", started, started + 120_000, status))


def _add_coverage(db, today, sources):
    ms = _ms(today)
    rows = [(source_id, "transcript", entity) for entity, source_id in sources.items()]
    rows += [(f"cli-{tool}", f"cli_{tool}", None) for tool in TOOLS]
    for source_id, source_type, entity in rows:
        db.execute(
            "INSERT INTO sources (source_id, source_type, entity_id, status, last_seen_ms) "
            "VALUES (?,?,?,?,?)",
            (source_id, source_type, entity, "ok", ms))
        db.execute(
            "INSERT INTO cursors (source_id, generation, byte_offset, last_seq, backlog_bytes, "
            "size_bytes, last_success_ms) VALUES (?,?,?,?,?,?,?)",
            (source_id, 0, 0, 0, 0, 48_000, ms))
    db.execute(
        "INSERT INTO collector_runs (started_at_ms, ended_at_ms, script_version, mode, runtime_ms, errors) "
        "VALUES (?,?,?,?,?,?)",
        (ms - 2_000, ms, "demo", "demo", 400, 0))


def _week_cents(rows, today, tool) -> int:
    start, end, _prev_start, _prev_end = budgets.week_bounds(today)
    total = 0
    for row in rows:
        if row["tool"] == tool and start <= row["day"] <= end:
            total += int(round(float(row["cost"]) * 100))
    return total


def populate(db, agents: Path, seed: int = 1, today=None) -> date:
    """Fill an open quota database and write SVG-only profiles under ``agents``."""
    today = _as_date(today)
    rng = random.Random(seed)
    cfg = budgets.load_budgets(budgets.DEFAULT_BUDGETS_PATH)
    day_cap = _cents(cfg["per_tool"]["daily_usd"]) * DAY_CAP_PERCENT // 100
    weekly_cents = _cents(cfg["per_tool"]["weekly_usd"])
    targets = {name: weekly_cents * TOOL_SHARE[name] // 100 for name in TOOLS}
    if targets[NEAR_TOOL] > 7 * day_cap:
        raise RuntimeError("weekly target does not fit under the daily cap")
    _write_profiles(agents, BOTS)
    bot_ids = []
    sources = {}
    first = _dates_ending(today, HISTORY_DAYS)[0]
    for number, name, _color, _shape, _kind in BOTS:
        entity = bot_id(number)
        bot_ids.append(entity)
        source_id = f"agent-transcripts/{entity}/{entity}.jsonl"
        sources[entity] = source_id
        db.execute(
            "INSERT INTO entities (entity_id, kind, display_name, slug, first_seen_ms, last_seen_ms, "
            "profile_present, active) VALUES (?,?,?,?,?,?,1,1)",
            (entity, "bot", name, name.lower(), _ms(first), _ms(today)))
    _add_coverage(db, today, sources)
    _add_cli(db, rng, today, bot_ids, day_cap)
    _add_activity(db, rng, today, BOTS, sources)
    _add_routines(db, today, bot_ids)
    db.execute("INSERT INTO usage_readings (day, usage_pct, recorded_at_ms) VALUES (?,?,?)",
               (today.isoformat(), 42, _ms(today)))
    db.commit()
    rows = budgets.rows_from_db(db)
    for tool, target in targets.items():
        got = _week_cents(rows, today, tool)
        if got != target:
            raise RuntimeError(f"{tool} week is {got} cents, expected {target}")
    breaches = budgets.find_breaches(rows, cfg, today)
    if breaches:
        raise RuntimeError("demo spend exceeds a budget: " + budgets.format_breach(breaches[0]))
    start, end, _prev_start, _prev_end = budgets.week_bounds(today)
    week_rows = [row for row in rows if start <= row["day"] <= end]
    total = sum(float(row["cost"]) for row in week_rows)
    unattr = sum(float(row["cost"]) for row in week_rows
                 if (row.get("bot_id") or "unattributed") == "unattributed")
    if total <= 0 or unattr / total <= 0.20:
        raise RuntimeError(f"demo unattributed share is {unattr / total if total else 0:.1%}")
    found = dashboard.page_alerts(db, today, today)
    kinds = [item["kind"] for item in found]
    if not 1 <= len(found) <= 2 or "budget" not in kinds or "unattributed" not in kinds:
        detail = "; ".join(item["text"] for item in found) or "none"
        raise RuntimeError(f"demo alerts drifted: {detail}")
    if any(item.get("level") == "over" for item in found):
        raise RuntimeError("demo should warn, not show a budget overage")
    return today


def build(db_path, out_path, seed: int = 1, today=None, agents=None) -> str:
    """Write the fake database and the dashboard HTML. Returns the HTML."""
    db_path = Path(db_path)
    out_path = Path(out_path)
    _reset_sqlite(db_path)
    owned_agents = agents is None
    agents_dir = Path(agents) if agents is not None else Path(tempfile.mkdtemp(prefix="quota-demo-agents-"))
    db = collect.open_db(str(db_path))
    try:
        day = populate(db, agents_dir, seed=seed, today=today)
        html = dashboard.build_html(db, agents=str(agents_dir), now=day)
    finally:
        db.close()
        if owned_agents:
            for child in agents_dir.iterdir():
                for item in child.iterdir():
                    item.unlink()
                child.rmdir()
            agents_dir.rmdir()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.name + ".tmp")
    temporary.write_text(html, encoding="utf-8")
    os.replace(temporary, out_path)
    return html


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="quota sqlite path (default: a temp file)")
    parser.add_argument("--out", default=None, help="dashboard HTML (default: docs/demo/dashboard.html)")
    parser.add_argument("--seed", type=int, default=1, help="deterministic variation seed")
    args = parser.parse_args(argv)
    db_path = Path(args.db) if args.db else default_db_path()
    out_path = Path(args.out) if args.out else default_out_path()
    try:
        html = build(db_path, out_path, seed=args.seed)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Demo database: {db_path}")
    print(f"Demo dashboard: {out_path} ({len(html.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

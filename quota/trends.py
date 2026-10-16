"""Dashboard series: CLI spend, bot scores, and collector freshness.

The aggregations are pure. ``load_dashboard_trends`` is the thin DB wrapper.
Weeks are Europe/Lisbon, seven days ending on ``today``, then the seven before.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from quota.budgets import lisbon_day_of, lisbon_today, week_bounds
from quota.prices import effective_cost


def _as_day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def weekly_cli_spend(rows, today: date) -> list[dict]:
    """This 7 days vs the previous 7, per tool.

    Each row is ``tool``, ``day``, ``cost``, ``estimated``. Tools with no spend
    in either window are omitted. Order is by tool name.
    """
    this_start, this_end, prev_start, prev_end = week_bounds(today)
    acc: dict[str, dict] = {}
    for row in rows:
        day = _as_day(row.get("day"))
        if day is None or day < prev_start or day > this_end:
            continue
        tool = row.get("tool")
        if not tool:
            continue
        slot = acc.setdefault(tool, {
            "tool": tool,
            "this_usd": 0.0,
            "prev_usd": 0.0,
            "this_estimated": False,
            "prev_estimated": False,
        })
        cost = float(row.get("cost") or 0)
        estimated = bool(row.get("estimated")) and cost != 0
        if this_start <= day <= this_end:
            slot["this_usd"] += cost
            slot["this_estimated"] = slot["this_estimated"] or estimated
        else:
            slot["prev_usd"] += cost
            slot["prev_estimated"] = slot["prev_estimated"] or estimated
    return [acc[name] for name in sorted(acc)]


def change_pct(this: float, prev: float):
    """Percent change. None when the previous week is 0 and this week is not."""
    if prev == 0:
        return None if this != 0 else 0.0
    return (this - prev) / prev * 100.0


def format_change(pct) -> str | None:
    """``+X%`` / ``-X%`` / ``0%``. None stays None (no previous week to compare)."""
    if pct is None:
        return None
    rounded = int(round(pct))
    if rounded == 0:
        return "0%"
    sign = "+" if rounded > 0 else ""
    return f"{sign}{rounded}%"


def bot_score_trends(rows, today: date) -> list[dict]:
    """Per-bot inclusive score this week vs the previous week, plus a 7-day sparkline.

    Each row is ``entity_id``, ``day``, ``score``. ``daily`` is this week, oldest
    first, with missing days as 0. Sorted by this week's score, then id.
    """
    this_start, this_end, prev_start, prev_end = week_bounds(today)
    days = [this_start + timedelta(days=i) for i in range(7)]
    acc: dict[str, dict] = {}
    for row in rows:
        day = _as_day(row.get("day"))
        if day is None or day < prev_start or day > this_end:
            continue
        eid = row.get("entity_id")
        if not eid:
            continue
        slot = acc.setdefault(eid, {"this": 0.0, "prev": 0.0, "by_day": {}})
        score = float(row.get("score") or 0)
        if this_start <= day <= this_end:
            slot["this"] += score
            slot["by_day"][day] = slot["by_day"].get(day, 0.0) + score
        else:
            slot["prev"] += score
    out = []
    for eid, slot in acc.items():
        pct = change_pct(slot["this"], slot["prev"])
        out.append({
            "entity_id": eid,
            "this_score": slot["this"],
            "prev_score": slot["prev"],
            "change_pct": pct,
            "change_label": format_change(pct),
            "days": [d.isoformat() for d in days],
            "daily": [slot["by_day"].get(d, 0.0) for d in days],
        })
    out.sort(key=lambda item: (-item["this_score"], item["entity_id"]))
    return out


def freshness_info(last_collect_ms, max_search_index_message_ms) -> dict:
    """Last collector finish time and the newest search-index message timestamp."""
    def ms(value):
        if value is None:
            return None
        return int(value)

    return {
        "last_collect_ms": ms(last_collect_ms),
        "max_search_index_message_ms": ms(max_search_index_message_ms),
    }


def _table(db, name, kind) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? AND type=?", (name, kind)).fetchone()
    return row is not None


def load_dashboard_trends(db, today=None) -> dict:
    """CLI spend, bot scores and freshness for the dashboard."""
    if today is None:
        today = lisbon_today()
    elif isinstance(today, datetime):
        today = today.date()
    this_start, this_end, prev_start, prev_end = week_bounds(today)
    cli_rows = []
    if _table(db, "cli_runs", "table"):
        cols = {row[1] for row in db.execute("PRAGMA table_info(cli_runs)")}
        est = "cost_usd_est" if "cost_usd_est" in cols else "NULL"
        flag = "COALESCE(cost_estimated,0)" if "cost_estimated" in cols else "0"
        sql = f"SELECT tool, ts, cost_usd, {est}, {flag} FROM cli_runs"
        for tool, ts, cost_usd, cost_est, flag_v in db.execute(sql):
            day = lisbon_day_of(ts)
            cost = effective_cost(cost_usd, cost_est)
            if day is None or cost is None or not tool:
                continue
            cli_rows.append({
                "tool": tool,
                "day": day,
                "cost": float(cost),
                "estimated": cost_usd is None and bool(flag_v),
            })
    score_rows = []
    if _table(db, "v_daily_entity", "view"):
        try:
            fetched = db.execute(
                "SELECT entity_id, day, score FROM v_daily_entity "
                "WHERE scope='inclusive' AND day!='undated'")
        except sqlite3.Error:
            fetched = []
        for entity, day, score in fetched:
            score_rows.append({"entity_id": entity, "day": day, "score": score or 0})
    last = None
    max_msg = None
    if _table(db, "collector_runs", "table"):
        last = db.execute(
            "SELECT MAX(COALESCE(ended_at_ms, started_at_ms)) FROM collector_runs").fetchone()[0]
    if _table(db, "events", "table") and _table(db, "cursors", "table"):
        max_msg = db.execute(
            "SELECT MAX(e.occurred_at_ms) FROM events e "
            "JOIN cursors c ON c.source_id=e.source_id AND c.generation=e.generation "
            "WHERE e.source_id LIKE 'search-index.db#messages%' AND e.event_type='message'"
        ).fetchone()[0]
    return {
        "today": today.isoformat(),
        "this_week": [this_start.isoformat(), this_end.isoformat()],
        "prev_week": [prev_start.isoformat(), prev_end.isoformat()],
        "cli_by_tool": weekly_cli_spend(cli_rows, today),
        "bots": bot_score_trends(score_rows, today),
        "freshness": freshness_info(last, max_msg),
    }

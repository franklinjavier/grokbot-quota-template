"""Daily and weekly CLI spend limits in Europe/Lisbon.

``per_tool`` includes every bot, including ``unattributed``. ``per_bot`` skips
``unattributed``. A week is the last 7 Lisbon days ending today. Spend is
``effective_cost`` (reported ``cost_usd``, else ``cost_usd_est``).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from quota.prices import effective_cost

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BUDGETS_PATH = os.path.join(ROOT, "config", "budgets.json")
TZ = ZoneInfo("Europe/Lisbon")
_LIMIT_KEYS = ("daily_usd", "weekly_usd")


def lisbon_today(now=None) -> date:
    if now is None:
        now = datetime.now(TZ)
    elif isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(TZ)
    elif isinstance(now, date):
        return now
    return now.date()


def lisbon_day_of(ts) -> date | None:
    """Lisbon calendar day of an ISO timestamp, or None."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    text = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).date()


def week_bounds(today: date) -> tuple[date, date, date, date]:
    """``this_start, this_end, prev_start, prev_end``. Each span is 7 days."""
    this_end = today
    this_start = today - timedelta(days=6)
    prev_end = this_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)
    return this_start, this_end, prev_start, prev_end


def load_budgets(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("budgets")
    for key in ("per_tool", "per_bot"):
        block = data.get(key)
        if not isinstance(block, dict):
            raise ValueError(f"budgets: {key}")
        for field in _LIMIT_KEYS:
            value = block.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"budgets: {key}.{field}")
    overrides = data.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("budgets: overrides")
    data["overrides"] = {
        "tools": overrides.get("tools") if isinstance(overrides.get("tools"), dict) else {},
        "bots": overrides.get("bots") if isinstance(overrides.get("bots"), dict) else {},
    }
    return data


def _limit_num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def limits_for(cfg: dict, kind: str, name: str) -> dict:
    """Default per-tool or per-bot limits, with a partial override merged in."""
    base_key = "per_tool" if kind == "tool" else "per_bot"
    group = "tools" if kind == "tool" else "bots"
    base = cfg.get(base_key) or {}
    extra = (cfg.get("overrides") or {}).get(group) or {}
    over = extra.get(name) if isinstance(extra.get(name), dict) else {}
    out = {}
    for key in _LIMIT_KEYS:
        picked = _limit_num(over.get(key))
        if picked is None:
            picked = _limit_num(base.get(key))
        if picked is not None:
            out[key] = picked
    return out


def _cents(value) -> int:
    return int(round(float(value) * 100))


def _day(value) -> date | None:
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


def find_breaches(rows, cfg: dict, today: date) -> list[dict]:
    """Spend rows compared with limits.

    Each row is ``tool``, ``bot_id``, ``day``, ``cost``, ``estimated``.
    ``unattributed`` counts toward the tool and not toward a bot.
    """
    this_start, this_end, _prev_start, _prev_end = week_bounds(today)
    tools: dict[str, dict] = {}
    bots: dict[str, dict] = {}

    def slot():
        return {"daily": 0.0, "weekly": 0.0, "daily_est": False, "weekly_est": False}

    for row in rows:
        day = _day(row.get("day"))
        if day is None or day < this_start or day > this_end:
            continue
        cost = float(row.get("cost") or 0)
        estimated = bool(row.get("estimated")) and cost != 0
        tool = row.get("tool")
        if not tool:
            continue
        bucket = tools.setdefault(tool, slot())
        bucket["weekly"] += cost
        bucket["weekly_est"] = bucket["weekly_est"] or estimated
        if day == today:
            bucket["daily"] += cost
            bucket["daily_est"] = bucket["daily_est"] or estimated
        bot = row.get("bot_id") or "unattributed"
        if bot == "unattributed":
            continue
        bot_bucket = bots.setdefault(bot, slot())
        bot_bucket["weekly"] += cost
        bot_bucket["weekly_est"] = bot_bucket["weekly_est"] or estimated
        if day == today:
            bot_bucket["daily"] += cost
            bot_bucket["daily_est"] = bot_bucket["daily_est"] or estimated

    found = []

    def emit(kind, name, window, spend, limit, start, end, estimated):
        if limit is None or _cents(spend) <= _cents(limit):
            return
        found.append({
            "kind": kind, "name": name, "window": window,
            "spend": spend, "limit": limit, "start": start, "end": end,
            "estimated": estimated,
        })

    for name in tools:
        limits = limits_for(cfg, "tool", name)
        bucket = tools[name]
        emit("tool", name, "daily", bucket["daily"], limits.get("daily_usd"), today, today, bucket["daily_est"])
        emit("tool", name, "weekly", bucket["weekly"], limits.get("weekly_usd"),
             this_start, this_end, bucket["weekly_est"])
    for name in bots:
        limits = limits_for(cfg, "bot", name)
        bucket = bots[name]
        emit("bot", name, "daily", bucket["daily"], limits.get("daily_usd"), today, today, bucket["daily_est"])
        emit("bot", name, "weekly", bucket["weekly"], limits.get("weekly_usd"),
             this_start, this_end, bucket["weekly_est"])
    found.sort(key=lambda b: ({"tool": 0, "bot": 1}[b["kind"]], b["name"], {"daily": 0, "weekly": 1}[b["window"]]))
    return found


def format_breach(b: dict) -> str:
    suffix = " (estimated)" if b.get("estimated") else ""
    return (f"EXCESSIVE: {b['kind']} {b['name']} {b['window']} "
            f"${b['spend']:.2f} > ${b['limit']:.2f} "
            f"({b['start'].isoformat()}..{b['end'].isoformat()}){suffix}")


def format_lines(found) -> list[str]:
    if not found:
        return ["budgets: ok"]
    return [format_breach(b) for b in found]


def rows_from_db(db) -> list[dict]:
    """Effective CLI spend rows. Missing estimate columns are treated as absent."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cli_runs'").fetchone():
        return []
    cols = {row[1] for row in db.execute("PRAGMA table_info(cli_runs)")}
    est = "cost_usd_est" if "cost_usd_est" in cols else "NULL"
    flag = "COALESCE(cost_estimated,0)" if "cost_estimated" in cols else "0"
    out = []
    sql = f"SELECT tool, bot_id, ts, cost_usd, {est}, {flag} FROM cli_runs"
    for tool, bot, ts, cost_usd, cost_est, flag_v in db.execute(sql):
        cost = effective_cost(cost_usd, cost_est)
        day = lisbon_day_of(ts)
        if cost is None or day is None or not tool:
            continue
        out.append({
            "tool": tool,
            "bot_id": bot or "unattributed",
            "day": day,
            "cost": float(cost),
            "estimated": cost_usd is None and bool(flag_v),
        })
    return out


def report_lines(db, path=None, today=None) -> list[str]:
    """Lines for the text summary. Missing config is reported, not raised."""
    path = path or DEFAULT_BUDGETS_PATH
    try:
        cfg = load_budgets(path)
    except (OSError, ValueError):
        return ["budgets: unavailable"]
    found = find_breaches(rows_from_db(db), cfg, today or lisbon_today())
    return format_lines(found)

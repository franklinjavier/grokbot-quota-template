"""Dashboard alerts. Pure rules, no database and no HTML.

Each alert is ``problem — suggested action``. ``build_alerts`` returns at
most six, highest severity first. ``label``, ``value``, and ``detail`` are
the short KPI parts of the same facts.

Budget: over the limit, or at least 80% of it (daily or weekly, tool or bot).
Spike: today, or this week, is more than twice the trailing average.
Paused: a paused tool or routine was used.
Unattributed: more than 20% of this week's CLI spend has no bot.
Stale: collector or search-index freshness text supplied by the caller.
"""

from __future__ import annotations

import re

NEAR = 0.80
UNATTRIBUTED = 0.20
SPIKE = 2.0
MAX_ALERTS = 6


def _cents(value) -> int:
    return int(round(float(value) * 100))


def classify_spend(spend, limit):
    """``over`` when spend is past the limit, ``near`` at 80% and up, else None.

    A missing limit is not a breach. Equality with the limit is not ``over``
    (same cents rule as the budget report) but it is ``near``.
    """
    if limit is None:
        return None
    spend_c = _cents(spend or 0)
    limit_c = _cents(limit)
    if spend_c > limit_c:
        return "over"
    if limit_c > 0 and spend_c >= _cents(float(limit) * NEAR):
        return "near"
    return None


def _money(value) -> str:
    return f"${float(value):,.2f}"


def _pct_text(share) -> str:
    pct = round(float(share) * 100, 1)
    if abs(pct - round(pct)) < 0.05:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%"


def _alert(severity, kind, problem, action, rank, **extra):
    item = {
        "severity": severity,
        "kind": kind,
        "problem": problem,
        "action": action,
        "text": f"{problem} — {action}",
        "_rank": rank,
    }
    item.update(extra)
    return item


def _budget_action(row) -> str:
    if row.get("kind") == "tool":
        return "switch tool or adjust the budget"
    routine = row.get("routine")
    if routine:
        return f"pause or adjust routine {routine}"
    return "adjust the budget"


def _budget_alerts(rows) -> list[dict]:
    pending = []
    for row in rows or []:
        level = classify_spend(row.get("spend") or 0, row.get("limit"))
        if level is None:
            continue
        spend = float(row.get("spend") or 0)
        limit = row.get("limit")
        limit_f = float(limit or 0)
        ratio = spend / limit_f if limit_f else float("inf")
        pending.append((level, ratio, row))

    def sort_key(item):
        level, ratio, row = item
        window_rank = 0 if row.get("window") == "daily" else 1
        kind_rank = 0 if row.get("kind") == "tool" else 1
        label = str(row.get("label") or row.get("name") or "")
        return (-ratio, window_rank, kind_rank, label)

    pending.sort(key=sort_key)
    found = []
    for level, _ratio, row in pending:
        label = row.get("label") or row.get("name") or "budget"
        window = row.get("window") or "weekly"
        spend = float(row.get("spend") or 0)
        limit_f = float(row.get("limit") or 0)
        pct = int(round(spend / limit_f * 100)) if limit_f else 0
        est = " (estimated)" if row.get("estimated") else ""
        problem = f"{label} {window} spend is {_money(spend)}{est} of {_money(limit_f)} ({pct}%)"
        severity = "high" if level == "over" else "medium"
        # Overs outrank every other high alert. Nears sit with the medium group.
        group = 0 if level == "over" else 3
        shown = f"{label} {window}"
        if shown[:1].islower():
            shown = shown[:1].upper() + shown[1:]
        found.append(_alert(
            severity, "budget", problem, _budget_action(row),
            (group, sort_key((level, _ratio, row))), level=level,
            label=shown,
            value=f"{pct}%",
            detail=f"{_money(spend)}{est} of {_money(limit_f)}"))
    return found


def _spike_alert(data) -> dict | None:
    candidates = []
    today_spend = float(data.get("today_spend") or 0)
    daily_avg = data.get("trailing_daily_avg")
    if daily_avg:
        daily_avg = float(daily_avg)
        if daily_avg > 0 and today_spend > SPIKE * daily_avg:
            candidates.append((today_spend / daily_avg, "today", data.get("top_task_today")))
    week_spend = float(data.get("week_spend") or 0)
    week_base = data.get("trailing_week_spend")
    if week_base:
        week_base = float(week_base)
        if week_base > 0 and week_spend > SPIKE * week_base:
            candidates.append((week_spend / week_base, "week", data.get("top_task_week")))
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    ratio, scope, task = candidates[0]
    when = "today" if scope == "today" else "this week"
    task_name = task or "unknown"
    problem = f"Spend spike: {when} is {ratio:.1f}× the trailing average"
    action = f"check for a runaway task: {task_name}"
    return _alert(
        "medium", "spike", problem, action, (4, ""),
        label="Spend spike", value=f"{ratio:.1f}×",
        detail=f"{when} vs trailing average")


def build_alerts(data: dict | None) -> list[dict]:
    """Severity-ordered alerts, at most six.

    ``data`` keys (all optional):

    * ``budgets``: ``kind`` (tool|bot), ``name``, ``label``, ``window``
      (daily|weekly), ``spend``, ``limit``, ``estimated``, ``routine``
    * ``today_spend``, ``trailing_daily_avg``, ``week_spend``,
      ``trailing_week_spend``, ``top_task_today``, ``top_task_week``
    * ``paused_in_use``: ``kind`` (tool|routine), ``name`` — already limited
      to paused things that were used
    * ``unattributed_share``: fraction of this week's CLI spend, or None
    * ``stale_parts``: phrases such as ``last collect 31 h ago (14/10 08:48)``
    """
    data = data or {}
    found = []
    found.extend(_budget_alerts(data.get("budgets") or []))
    for row in data.get("paused_in_use") or []:
        name = row.get("name") or "item"
        if row.get("kind") == "routine":
            problem = f"Paused routine {name} was used"
        else:
            problem = f"Paused tool {name} was used"
        kind_name = "routine" if row.get("kind") == "routine" else "tool"
        found.append(_alert(
            "high", "paused", problem, "stop it or re-enable on purpose", (1, str(name)),
            label=f"Paused {kind_name}", value=str(name), detail="was used"))
    stale_parts = [part for part in (data.get("stale_parts") or []) if part]
    if stale_parts:
        problem = "Stale data: " + "; ".join(stale_parts)
        hours = []
        for part in stale_parts:
            match = re.search(r"(\d+) h ago", part)
            if match:
                hours.append(int(match.group(1)))
        stale_value = f"{max(hours)} h" if hours else "stale"
        found.append(_alert(
            "high", "stale", problem, "check the routine/collector", (2, ""),
            label="Stale data", value=stale_value, detail="; ".join(stale_parts)))
    spike = _spike_alert(data)
    if spike:
        found.append(spike)
    share = data.get("unattributed_share")
    if share is not None and float(share) > UNATTRIBUTED:
        problem = f"Unattributed CLI spend is {_pct_text(share)} this week"
        found.append(_alert(
            "medium", "unattributed", problem, "adopt grokbot-run", (5, ""),
            label="Unattributed", value=_pct_text(share), detail="of this week's CLI spend"))
    found.sort(key=lambda item: item["_rank"])
    out = []
    for item in found[:MAX_ALERTS]:
        cleaned = {key: value for key, value in item.items() if key != "_rank"}
        out.append(cleaned)
    return out

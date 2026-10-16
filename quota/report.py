#!/usr/bin/env python3
"""Static, self-contained HTML panel + short text summary for the quota DB.

Usage:
  python3 quota/report.py [--db PATH] [--out PATH] [--start YYYY-MM-DD] [--end YYYY-MM-DD]

The report window defaults to the last 7 days ending today; if nothing is
dated there it falls back to the 7 days ending at the latest active day, then
to all time. An explicit --start/--end is used as given.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota import budgets, paths, trends  # noqa: E402
from quota.attribute import TZ  # noqa: E402
SCORE_FORMULA = ("score-v2 = 1.0×inference_steps + 0.5×tool_calls + 5×child_launches + 0.2×computer_actions "
                 "+ 1×screenshots + 1×root_requests + 1×proxy_replies + observed_text_bytes/4000")
SEARCH_INDEX_LIKE = "search-index.db#%"
MIB = 1024 * 1024


def q(db, sql, args=()):
    return db.execute(sql, args).fetchall()


def names(db) -> dict[str, str]:
    out = {}
    for eid, kind, name in q(db, "SELECT entity_id, kind, display_name FROM entities"):
        if name:
            out[eid] = name
        elif eid.startswith("sand-subagent-"):
            out[eid] = "child " + eid[len("sand-subagent-"):][:8]
        else:
            out[eid] = ("unnamed " if kind == "bot" else "") + eid[:8]
    return out


def fmt(v):
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def table(headers, rows, cls=""):
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(fmt(c))}</td>" for c in r) + "</tr>" for r in rows)
    if not rows:
        body = f"<tr><td colspan='{len(headers)}' class='muted'>no data</td></tr>"
    return f"<table class='{cls}'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def ms_str(ms):
    if not ms:
        return "–"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def today() -> date:
    return datetime.now(TZ).date()


def anchor(db) -> date:
    """Latest Lisbon day with positive proxy activity (capped at today)."""
    row = q(db, "SELECT MAX(day) FROM v_daily_entity WHERE day != 'undated' AND score > 0")[0][0]
    return min(today(), date.fromisoformat(row)) if row else today()


METRICS = ("root_requests, inference_steps, tool_calls, child_launches, cu_actions, screenshots, proxy_replies, "
           "bytes, score")


def top_by_day(db, days=14, n=5, scope="inclusive"):
    since = (anchor(db) - timedelta(days=days - 1)).isoformat()
    rows = q(db, f"SELECT day, entity_id, {METRICS} FROM v_daily_entity WHERE scope=? AND day>=? AND day!='undated' "
                 "AND score > 0 "
                 "ORDER BY day DESC, score DESC", (scope, since))
    out, per = [], defaultdict(int)
    for r in rows:
        if per[r[0]] < n:
            per[r[0]] += 1
            out.append(r)
    return out


def top_by_week(db, weeks=6, n=8, scope="inclusive"):
    rows = q(db, f"SELECT day, entity_id, {METRICS} FROM v_daily_entity WHERE scope=? AND day!='undated' AND score > 0",
             (scope,))
    agg = defaultdict(lambda: [0] * 9)
    for r in rows:
        y, w, _ = date.fromisoformat(r[0]).isocalendar()
        acc = agg[(f"{y}-W{w:02d}", r[1])]
        for i, v in enumerate(r[2:]):
            acc[i] += v or 0
    weeks_sorted = sorted({k[0] for k in agg}, reverse=True)[:weeks]
    out = []
    for wk in weeks_sorted:
        items = sorted(((e, v) for (k, e), v in agg.items() if k == wk), key=lambda x: -x[1][8])[:n]
        out += [(wk, e, *v) for e, v in items]
    return out


def window_top(db, start: date | None, end: date | None, n=5):
    """Top causal owners in [start, end] (all time when start is None)."""
    sql = ("SELECT entity_id, SUM(score), SUM(inference_steps), SUM(tool_calls), SUM(child_launches), "
           "SUM(proxy_replies), SUM(search_index_events) FROM v_daily_entity WHERE scope='inclusive' ")
    args: tuple = ()
    if start:
        sql += "AND day BETWEEN ? AND ? "
        args = (start.isoformat(), end.isoformat())
    return q(db, sql + "GROUP BY 1 HAVING SUM(score) > 0 ORDER BY 2 DESC LIMIT ?", (*args, n))


def report_window(db, start: date | None = None, end: date | None = None) -> tuple:
    """(start, end, label). Default: the last 7 days ending today, falling back
    only if that is empty. An explicit --start/--end is never replaced."""
    if start or end:
        end = end or today()
        start = start or end - timedelta(days=6)
        return start, end, f"{start}..{end}"
    end = today()
    if window_top(db, end - timedelta(days=6), end, 1):
        return end - timedelta(days=6), end, f"{end - timedelta(days=6)}..{end} (last 7 days)"
    last = anchor(db)
    if window_top(db, last - timedelta(days=6), last, 1):
        return (last - timedelta(days=6), last,
                f"{last - timedelta(days=6)}..{last} (fallback: nothing dated in the last 7 days)")
    return None, None, "all time (fallback: nothing dated in the last 7 days)"


def source_coverage(db) -> list[tuple]:
    """Per source type: sources, events, first/last estimated time, last exact time
    (activity only; configuration edits are left out)."""
    return q(db, "SELECT s.source_type, COUNT(DISTINCT e.source_id), COUNT(*), MIN(e.est_at_ms), MAX(e.est_at_ms), "
                 "MAX(e.occurred_at_ms) FROM v_canonical_activity e JOIN sources s ON s.source_id = e.source_id "
                 "WHERE e.event_type != 'configuration_changed' "
                 "GROUP BY 1 ORDER BY 5 DESC")


def coverage_ends(db) -> tuple:
    """(last detailed transcript/store time, last search-index time)."""
    detailed = q(db, "SELECT MAX(est_at_ms) FROM v_canonical_activity WHERE event_type != 'configuration_changed' "
                     "AND (source_id LIKE 'agent-transcripts/%' OR source_id LIKE 'agents/%/store.db')")[0][0]
    mirror = q(db, "SELECT MAX(est_at_ms) FROM events WHERE source_id LIKE ?", (SEARCH_INDEX_LIKE,))[0][0]
    return detailed, mirror


def reply_calibration(db) -> tuple:
    """On entity-days with both transcript inference steps and search-index
    assistant messages: (steps, replies, entity-days)."""
    return q(db, "WITH inf AS (SELECT entity_id, est_day, COUNT(*) n FROM v_canonical_activity "
                 "WHERE event_type='inference_step' AND source_id LIKE 'agent-transcripts/%' AND est_day IS NOT NULL "
                 "GROUP BY 1, 2), rep AS (SELECT entity_id, est_day, COUNT(*) n FROM events "
                 "WHERE source_id LIKE ? AND event_type='message' AND status='assistant' AND est_day IS NOT NULL "
                 "GROUP BY 1, 2) SELECT COALESCE(SUM(inf.n),0), COALESCE(SUM(rep.n),0), COUNT(*) "
                 "FROM inf JOIN rep USING (entity_id, est_day)", (SEARCH_INDEX_LIKE,))[0]


def flags(db, nm) -> list[tuple]:
    """Deterministic excess heuristics -> (flag, subject, evidence, confidence)."""
    out = []
    since = (anchor(db) - timedelta(days=13)).isoformat()
    since_ms = int(datetime.combine(anchor(db) - timedelta(days=13), datetime.min.time(), TZ).timestamp() * 1000)
    # Frequent automation / failures / overlaps.
    for owner, rid, name, enabled, sched in q(db, "SELECT owner_id, routine_id, name, enabled, schedule FROM routines"):
        runs = q(db, "SELECT run_id, started_at_ms, finished_at_ms, status FROM runs WHERE owner_id=? AND routine_id=? "
                     "ORDER BY started_at_ms", (owner, rid))
        recent = [r for r in runs if (r[1] or 0) >= since_ms]
        subj = f"{nm.get(owner, owner)} / {name or rid}"
        parts = (sched or "").split()
        multi = len(parts) == 5 and enabled and ("," in parts[1] or parts[1] == "*" or "/" in parts[1])
        if len(recent) / 14 > 1.5 or multi:
            out.append(("frequent_automation", subj, f"schedule '{sched}', {len(recent)} runs in 14d", "high"))
        errs = [r[0] for r in runs if r[3] not in ("ok", None)]
        if errs:
            out.append(("failed_routine_runs", subj, f"{len(errs)} non-ok runs, e.g. {errs[-1][:8]}", "high"))
        for a, b in zip(runs, runs[1:]):
            if a[2] and b[1] and b[1] < a[2]:
                out.append(("overlapping_runs", subj, f"run {b[0][:8]} started before {a[0][:8]} finished", "high"))
    # Per entity-day heuristics on canonical events.
    rows = q(db, "SELECT COALESCE(entity_id,'unknown'), est_day, "
                 "SUM(tool_category IN ('browser','computer') AND event_type='tool_called') + SUM(actions) + SUM(screenshots) "
                 "+ SUM(event_type='audit_browser_navigation'), "
                 "SUM(result_bytes), SUM(event_type='tool_result'), "
                 "SUM(event_type='tool_result' AND status='error') "
                 "FROM v_canonical_activity WHERE est_day >= ? GROUP BY 1, 2", (since,))
    for ent, day, browser, rbytes, results, errors in rows:
        if browser >= 150:
            out.append(("browser_churn", nm.get(ent, ent), f"{day}: {browser} browser/computer actions+screenshots", "medium"))
        if rbytes >= 5 * MIB:
            out.append(("tool_context_bulk", nm.get(ent, ent), f"{day}: {rbytes / MIB:.1f} MiB tool results", "medium"))
        if results >= 20 and errors / results > 0.2:
            out.append(("retry_failure_amplification", nm.get(ent, ent), f"{day}: {errors}/{results} tool results errored", "medium"))
    for root, day, kids in q(db, "SELECT root_entity_id, est_day, SUM(tool_category='subagent' AND event_type='tool_called') "
                                 "FROM v_canonical_activity WHERE est_day >= ? GROUP BY 1, 2", (since,)):
        if kids >= 10:
            out.append(("delegation_amplification", nm.get(root, root), f"{day}: {kids} child launches/resumes", "medium"))
    for ent, n in q(db, "SELECT entity_id, COUNT(*) FROM v_canonical_activity WHERE event_type='tool_called' "
                        "AND tool_category='subagent' AND safe_metadata_json LIKE '%\"background\": true%' GROUP BY 1"):
        if n >= 5:
            out.append(("persistent_background_work", nm.get(ent, ent), f"{n} background child launches (all time)", "low"))
    # Message ping-pong between bots.
    pairs = defaultdict(int)
    for a, b, day, n in q(db, "SELECT peer_id, entity_id, est_day, COUNT(*) FROM v_canonical_activity "
                              "WHERE event_type='message' AND peer_id IS NOT NULL AND est_day >= ? GROUP BY 1, 2, 3", (since,)):
        pairs[(a, b, day)] = n
    for (a, b, day), n in pairs.items():
        back = pairs.get((b, a, day), 0)
        if a < b and back and n + back >= 20:
            out.append(("message_ping_pong", f"{nm.get(a, a)} ↔ {nm.get(b, b)}", f"{day}: {n}+{back} messages", "medium"))
    # Activity anomaly vs trailing 14-day median.
    series = defaultdict(dict)
    for ent, day, score in q(db, "SELECT entity_id, day, score FROM v_daily_entity WHERE scope='direct' AND day!='undated'"):
        series[ent][day] = score
    for ent, s in series.items():
        days = sorted(s)
        for d in days:
            if d < since:
                continue
            prev = [s.get((date.fromisoformat(d) - timedelta(days=i)).isoformat(), 0) for i in range(1, 15)]
            if sum(1 for v in prev if v) >= 7 and s[d] >= 50 and s[d] > 2 * statistics.median(prev):
                out.append(("activity_anomaly", nm.get(ent, ent),
                            f"{d}: score {s[d]:.0f} vs 14d median {statistics.median(prev):.0f}", "low"))
    # Tracker overhead.
    for day, secs in q(db, "SELECT date(started_at_ms/1000,'unixepoch'), SUM(runtime_ms)/1000.0 FROM collector_runs "
                           "WHERE mode='pass' GROUP BY 1 HAVING SUM(runtime_ms) > 300000"):
        out.append(("tracker_overhead", "collector", f"{day}: {secs:.0f}s collector runtime (budget 300s/day)", "high"))
    return out


CSS = """
body{font:14px/1.4 system-ui,sans-serif;margin:24px;color:#1d2330;background:#fafafa}
h1{font-size:20px}h2{font-size:16px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}
table{border-collapse:collapse;margin:8px 0;background:#fff}
th,td{border:1px solid #e1e3e8;padding:3px 8px;text-align:left;font-variant-numeric:tabular-nums}
th{background:#f0f2f5;position:sticky;top:0}td:not(:nth-child(-n+2)){text-align:right}
.muted{color:#777}.note{background:#fff7e0;border:1px solid #f0d890;padding:8px 12px;max-width:960px}
.grid{display:flex;gap:24px;flex-wrap:wrap}
"""


def build_html(db, window=None) -> str:
    nm = names(db)
    n = lambda e: nm.get(e, e or "unknown")  # noqa: E731
    gen = datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")
    parts = [f"<h1>Grok Bot quota tracker — usage proxies</h1><p class='muted'>Generated {gen}</p>",
             "<div class='note'><b>These are activity proxies, not quota consumption.</b> No source exposes real "
             "tokens, reasoning effort, model weighting or per-bot quota. Text bytes/4 is only a rough token "
             f"approximation (sensitivity ÷3…÷5). <br><code>{html.escape(SCORE_FORMULA)}</code><br>"
             "<i>inclusive</i> = activity of a bot plus its descendants (causal owner); <i>direct</i> = execution "
             "owner only. Transcript lines have no timestamps: backfilled lines are interpolated; see coverage.</div>"]

    # Coverage note: which source reaches how far.
    detailed, mirror = coverage_ends(db)
    steps, replies, days = reply_calibration(db)
    note = ("<div class='note'><b>Coverage.</b> Detailed sources (store.db, transcripts) end at "
            f"<b>{ms_str(detailed)}</b>; the search-index mirror reaches <b>{ms_str(mirror)}</b>. ")
    if mirror and (not detailed or mirror > detailed):
        note += ("After the detailed end only the search-index covers activity: those days score "
                 "1×assistant reply (<i>proxy_replies</i>, a lower bound for inference steps) + message text/4000, "
                 "with no tool calls, children, computer use or root requests, so they read <b>low</b> next to "
                 "earlier days. ")
    if replies:
        note += (f"On {days} entity-days covered by both, transcripts show {steps / replies:.1f} inference steps "
                 "per assistant reply. ")
    note += "Search-index rows already held by store.db (same agent + entry id) are not counted twice.</div>"
    parts.append(note + table(
        ["source type", "sources", "events", "first (est.)", "last (est.)", "last exact timestamp"],
        [(r[0], r[1], r[2], ms_str(r[3]), ms_str(r[4]), ms_str(r[5])) for r in source_coverage(db)]))

    def ranked(rows):
        return [(r[0], n(r[1]), *r[2:9], round((r[9] or 0) / MIB, 2), r[10]) for r in rows]

    start, end, label = window or report_window(db)
    parts.append(f"<h2>Top spenders in report window: {html.escape(label)}</h2>" + table(
        ["bot", "score", "inference", "tool calls", "children", "SI replies", "SI events"],
        [(n(r[0]), *r[1:]) for r in window_top(db, start, end, 15)]))
    parts.append(f"<p>Latest day with activity: <b>{anchor(db)}</b> (windows below end there).</p>")
    hdr = ["period", "bot", "requests", "inference", "tool calls", "children", "CU actions", "screenshots",
           "SI replies", "MiB text", "score"]
    parts.append("<h2>Top spenders by day (inclusive, last 14 days, Lisbon dates)</h2>" + table(hdr, ranked(top_by_day(db))))
    parts.append("<h2>Top spenders by ISO week (inclusive)</h2>" + table(hdr, ranked(top_by_week(db))))

    rows = q(db, "SELECT e.entity_id, e.kind, "
                 "COALESCE((SELECT SUM(score) FROM v_daily_entity d WHERE d.scope='direct' AND d.entity_id=e.entity_id),0), "
                 "COALESCE((SELECT SUM(score) FROM v_daily_entity d WHERE d.scope='inclusive' AND d.entity_id=e.entity_id),0), "
                 "COALESCE((SELECT SUM(score) FROM v_daily_entity d WHERE d.scope='inclusive' AND d.entity_id=e.entity_id AND d.day='undated'),0), "
                 "(SELECT COUNT(*) FROM links l WHERE l.from_id=e.entity_id AND l.relation='parent-child') "
                 "FROM entities e WHERE e.kind != 'child' ORDER BY 4 DESC")
    parts.append("<h2>All bots, all time (including zero activity)</h2>" + table(
        ["bot", "kind", "direct score", "inclusive score", "undated part", "children linked"],
        [(n(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]))

    # Routines.
    rrows = []
    for owner, rid, name, enabled, ttype, sched, pchars in q(
            db, "SELECT owner_id, routine_id, name, enabled, trigger_type, schedule, prompt_chars FROM routines "
                "ORDER BY owner_id, routine_id"):
        st = q(db, "SELECT COUNT(*), SUM(status='ok'), SUM(status!='ok'), AVG(duration_ms)/1000.0, SUM(linked_events), "
                   "SUM(audit_events), MAX(started_at_ms) FROM v_routine_runs WHERE owner_id=? AND routine_id=?",
               (owner, rid))[0]
        rrows.append((n(owner), name or rid, "yes" if enabled else "no", f"{ttype or ''} {sched or ''}".strip(), pchars,
                      st[0], st[1] or 0, st[2] or 0, round(st[3], 1) if st[3] else None, st[4] or 0, st[5] or 0,
                      ms_str(st[6])))
    parts.append("<h2>Routines</h2>" + table(
        ["owner", "routine", "enabled", "trigger", "prompt chars", "runs", "ok", "failed", "avg s", "linked events",
         "audit events", "last run"], rrows))
    recent = q(db, "SELECT owner_id, routine_id, run_id, status, started_at_ms, duration_ms, linked_events, tool_calls, "
                   "audit_events, attribution_confidence FROM v_routine_runs ORDER BY started_at_ms DESC LIMIT 40")
    parts.append("<h3>Recent routine runs</h3>" + table(
        ["owner", "routine", "run", "status", "started", "duration s", "linked events", "tool calls", "audit events",
         "confidence"],
        [(n(r[0]), r[1], r[2][:8], r[3], ms_str(r[4]), round(r[5] / 1000, 1) if r[5] else None, *r[6:]) for r in recent]))

    # Computer use.
    cu = q(db, "SELECT entity_id, COUNT(*), SUM(actions), SUM(screenshots), SUM(duration_ms)/1000.0 "
               "FROM v_canonical_activity WHERE event_type='computer_session' GROUP BY 1 ORDER BY 3 DESC")
    cut = q(db, "SELECT root_entity_id, SUM(tool_category='computer') c, SUM(tool_category='browser') b, "
                "COUNT(DISTINCT CASE WHEN tool_category IN ('computer','browser') THEN entity_id END) "
                "FROM v_canonical_activity WHERE event_type='tool_called' GROUP BY 1 HAVING c+b > 0 ORDER BY c+b DESC")
    parts.append("<h2>Computer-use and browser activity</h2><div class='grid'><div><h3>Audited computer-use sessions</h3>" +
                 table(["bot", "sessions", "actions", "screenshots", "duration s"], [(n(r[0]), *r[1:]) for r in cu]) +
                 "</div><div><h3>Transcript computer/browser tool calls (inclusive)</h3>" +
                 table(["causal owner", "computer calls", "browser calls", "executing agents"], [(n(r[0]), *r[1:]) for r in cut]) +
                 "</div></div>")
    top_sessions = q(db, "SELECT entity_id, est_at_ms, actions, screenshots, duration_ms FROM v_canonical_activity "
                         "WHERE event_type='computer_session' ORDER BY actions DESC LIMIT 15")
    parts.append("<h3>Largest computer-use sessions</h3>" + table(
        ["bot", "at", "actions", "screenshots", "duration s"],
        [(n(r[0]), ms_str(r[1]), r[2], r[3], round((r[4] or 0) / 1000, 1)) for r in top_sessions]))

    # MCP.
    mcp = q(db, "SELECT mcp_server, COUNT(*), SUM(status!='ok'), SUM(duration_ms)/1000.0, COUNT(DISTINCT entity_id) "
                "FROM v_canonical_activity WHERE event_type='audit_mcp_call' GROUP BY 1 ORDER BY 2 DESC LIMIT 20")
    mcpt = q(db, "SELECT COALESCE(mcp_server,'(unnamed)'), COUNT(*), SUM(argument_bytes), COUNT(DISTINCT root_entity_id) "
                 "FROM v_canonical_activity WHERE event_type='tool_called' AND tool_category='mcp' GROUP BY 1 "
                 "ORDER BY 2 DESC LIMIT 20")
    parts.append("<h2>MCP top servers</h2><div class='grid'><div><h3>Audit (has durations)</h3>" +
                 table(["server", "calls", "non-ok", "total s", "bots"], mcp) +
                 "</div><div><h3>Transcripts (CallMcpTool)</h3>" +
                 table(["server", "calls", "arg bytes", "causal owners"], mcpt) + "</div></div>")
    cats = q(db, "SELECT tool_category, COUNT(*), COUNT(DISTINCT tool_name), SUM(argument_bytes) "
                 "FROM v_canonical_activity WHERE event_type='tool_called' GROUP BY 1 ORDER BY 2 DESC")
    res = dict((r[0], r[1:]) for r in q(db, "SELECT tool_category, SUM(result_bytes), SUM(status='error') FROM "
                                            "v_canonical_activity WHERE event_type='tool_result' GROUP BY 1"))
    parts.append("<h3>Tool calls by category</h3>" + table(
        ["category", "calls", "distinct tools", "arg MiB", "result MiB", "errored results"],
        [(c, k, d, round((a or 0) / MIB, 2), round((res.get(c, (0, 0))[0] or 0) / MIB, 2), res.get(c, (0, 0))[1] or 0)
         for c, k, d, a in cats]))
    kids = q(db, "SELECT COALESCE(json_extract(safe_metadata_json,'$.subagent_type'),'(unknown)'), COUNT(*), "
                 "SUM(status='resume'), COUNT(DISTINCT model_label), GROUP_CONCAT(DISTINCT model_label) "
                 "FROM v_canonical_activity WHERE event_type='tool_called' AND tool_category='subagent' GROUP BY 1 ORDER BY 2 DESC")
    parts.append("<h3>Child launches by type</h3>" + table(
        ["subagent type", "launches", "resumes", "model labels", "labels (not billing models)"], kids) +
        "<p class='muted'>Effort level: not available in any source (stored as unknown).</p>")

    # Flags.
    fl = flags(db, nm)
    parts.append("<h2>Excess-usage flags (review hints, not verdicts)</h2>" +
                 table(["flag", "subject", "evidence", "confidence"], fl))

    # Coverage.
    cov = q(db, "SELECT source_type, sources, ok, missing, errored, ROUND(size_bytes/1048576.0,1), "
                "ROUND(backlog_bytes/1048576.0,2), sources_with_backlog, last_success_ms FROM v_coverage")
    parts.append("<h2>Coverage</h2>" + table(
        ["source type", "sources", "ok", "missing", "errored", "size MiB", "backlog MiB", "with backlog", "last success"],
        [(*r[:8], ms_str(r[8])) for r in cov]))
    tb = q(db, "SELECT time_basis, COUNT(*), SUM(est_at_ms IS NULL) FROM v_canonical_activity GROUP BY 1")
    ents = q(db, "SELECT kind, COUNT(*), SUM(profile_present) FROM entities GROUP BY 1")
    unl = q(db, "SELECT COUNT(*) FROM entities WHERE kind='child' AND entity_id NOT IN "
                "(SELECT to_id FROM links WHERE relation='parent-child')")[0][0]
    conf = q(db, "SELECT confidence, method, COUNT(*) FROM links GROUP BY 1, 2 ORDER BY 3 DESC")
    unk = q(db, "SELECT event_type, COUNT(*) FROM v_canonical_activity WHERE event_type IN "
                "('unknown','audit_unknown','coverage_gap') GROUP BY 1")
    errs = q(db, "SELECT source_id, error_code FROM cursors WHERE error_code IS NOT NULL LIMIT 20")
    parts.append("<div class='grid'><div><h3>Time basis</h3>" + table(["basis", "events", "undated"], tb) +
                 "</div><div><h3>Identities</h3>" + table(["kind", "count", "with profile"], ents) +
                 f"<p>Children with no known parent (Unknown / unassigned causal owner): <b>{unl}</b></p></div>"
                 "<div><h3>Links</h3>" + table(["confidence", "method", "links"], conf) +
                 "</div><div><h3>Unknown / gaps</h3>" + table(["type", "events"], unk) +
                 table(["source", "error"], errs) + "</div></div>")
    snaps = q(db, "SELECT COUNT(*) FROM quota_snapshots")[0][0]
    parts.append(f"<p class='muted'>Aggregate quota snapshots: {snaps} (no verified source yet).</p>")

    # Self-metering.
    cr = q(db, "SELECT started_at_ms, mode, runtime_ms, ROUND(bytes_read/1048576.0,2), rows_inspected, events_inserted, "
               "ROUND(backlog_bytes/1048576.0,2), errors, ROUND(db_bytes/1048576.0,1), report_bytes "
               "FROM collector_runs ORDER BY collector_run_id DESC LIMIT 15")
    tot = q(db, "SELECT COUNT(*), SUM(runtime_ms)/1000.0, SUM(bytes_read)/1048576.0 FROM collector_runs")[0]
    parts.append("<h2>Collector self-metering</h2>" +
                 f"<p>{tot[0]} collector runs, {fmt(tot[1])} s total runtime, {fmt(tot[2])} MiB read.</p>" + table(
                     ["started", "mode", "runtime ms", "MiB read", "rows", "events", "backlog MiB", "errors", "DB MiB",
                      "report bytes"], [(ms_str(r[0]), *r[1:]) for r in cr]))
    cli = cli_by_tool(db)
    parts.append("<h2>CLI runs</h2><p class='muted'>Claude, Codex and Grok CLI sessions. Not included in the "
                 "score.</p>")
    note = cli_attribution_text(db)
    if note:
        parts.append(f"<p class='muted'>{html.escape(note)}</p>")
    parts.append(table(
        ["tool", "runs", "cost_usd", "tokens"],
        [(t, n, panel_cli_cost(c, est), int(tok or 0)) for t, n, c, tok, est in cli]))
    parts.append("<h2>Limitations</h2><ul><li>The score is an activity proxy: effort and per-bot quota are not in "
                 "the Grok Bot sources. CLI runs are separate and are not included in the score; they do carry "
                 "token totals when the tool reports them. Claude and Grok report cost_usd. Codex "
                 "cost is an estimate from the price table (labelled estimated) and is stored beside "
                 "cost_usd, which stays empty.</li>"
                 "<li>Detailed transcripts have no timestamps; backfilled lines are "
                 "interpolated between a bot's first and last store/audit timestamps, children inherit the time of "
                 "the parent event that links them. Daily figures for transcript-derived metrics are approximate.</li>"
                 "<li>Resumed children are attributed to their first parent link only.</li><li>Audit shell/MCP/browser "
                 "events overlap transcript tool calls and are excluded from the score.</li><li>Deleted or remote "
                 "telemetry cannot be reconstructed.</li></ul>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Grok Bot quota tracker</title>"
            f"<style>{CSS}</style></head><body>{''.join(parts)}</body></html>")


def write_panel(db, path: str, window=None) -> int:
    data = build_html(db, window).encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    with db:
        db.execute("UPDATE collector_runs SET report_bytes=? WHERE collector_run_id=(SELECT MAX(collector_run_id) "
                   "FROM collector_runs)", (len(data),))
    return len(data)


CLI_TOKEN_SQL = ("COALESCE(input_tokens,0)+COALESCE(output_tokens,0)+COALESCE(cache_read_tokens,0)"
                 "+COALESCE(cache_create_tokens,0)+COALESCE(reasoning_tokens,0)")


def cli_by_tool(db):
    """(tool, runs, effective cost or None, tokens, estimated rows) per tool.

    Effective cost is cost_usd when the tool reported one, otherwise cost_usd_est.
    """
    if not q(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cli_runs'"):
        return []
    cols = {row[1] for row in q(db, "PRAGMA table_info(cli_runs)")}
    if "cost_usd_est" in cols:
        cost = "SUM(CASE WHEN cost_usd IS NOT NULL THEN cost_usd ELSE cost_usd_est END)"
        est = ("SUM(CASE WHEN cost_usd IS NULL AND COALESCE(cost_estimated,0)=1 "
               "AND cost_usd_est IS NOT NULL THEN 1 ELSE 0 END)")
    else:
        cost = "SUM(cost_usd)"
        est = "0"
    return q(db, f"SELECT tool, COUNT(*), {cost}, SUM({CLI_TOKEN_SQL}), {est} FROM cli_runs "
                 "GROUP BY tool ORDER BY tool")


def format_cli_cost(cost, estimated) -> str:
    """Summary cost. Estimates are suffixed `` (estimated)``."""
    if cost is None:
        return "unknown"
    text = f"{cost:.2f}"
    if estimated:
        text += " (estimated)"
    return text


def panel_cli_cost(cost, estimated) -> str:
    if cost is None:
        return "–"
    return format_cli_cost(cost, estimated)


def cli_attribution_text(db) -> str | None:
    """Small CLI-panel line of attribution_method counts, or None when there are no runs."""
    if not q(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cli_runs'"):
        return None
    if not any(r[1] == "attribution_method" for r in q(db, "PRAGMA table_info(cli_runs)")):
        return None
    if not q(db, "SELECT COUNT(*) FROM cli_runs")[0][0]:
        return None
    counts = dict(q(db, "SELECT COALESCE(attribution_method, 'unattributed'), COUNT(*) "
                        "FROM cli_runs GROUP BY 1"))
    order = ("launch_session", "launch_cwd", "plugin", "unattributed")
    bits = [f"{name} {int(counts.get(name, 0))}" for name in order]
    return "attribution: " + ", ".join(bits)


def cli_summary_lines(db) -> list[str]:
    lines = ["CLI runs (not included in the score):"]
    rows = cli_by_tool(db)
    if not rows:
        lines.append("  (none)")
        return lines
    for tool, runs, cost, tokens, estimated in rows:
        noun = "run" if runs == 1 else "runs"
        lines.append(f"  {tool}: {runs} {noun}, cost_usd {format_cli_cost(cost, estimated)}, "
                     f"tokens {int(tokens or 0)}")
    return lines


def summary(db, panel: str, window=None) -> str:
    nm = names(db)
    start, end, label = window or report_window(db)
    top = window_top(db, start, end, 5)
    backlog, srcs = q(db, "SELECT COALESCE(SUM(backlog_bytes),0), COALESCE(SUM(backlog_bytes>0),0) FROM cursors")[0]
    last = q(db, "SELECT runtime_ms, errors FROM collector_runs ORDER BY collector_run_id DESC LIMIT 1")
    lines = [f"SUMMARY usage proxies (score-v2, not quota) for {label}:"]
    for i, (e, score, inf, tools, kids, replies, si) in enumerate(top, 1):
        lines.append(f"  {i}. {nm.get(e, e)}: score {score:,.0f} (inference {inf:,}, tools {tools:,}, "
                     f"children {kids:,}, search-index replies {replies:,})")
    if not top:
        lines.append("  (no scored activity in this window)")
    detailed, mirror = coverage_ends(db)
    lines.append(f"  coverage: detailed sources end {ms_str(detailed)}; search-index through {ms_str(mirror)}")
    undated = q(db, "SELECT COALESCE(SUM(score),0) FROM v_daily_entity WHERE scope='inclusive' AND day='undated'")[0][0]
    total = q(db, "SELECT COALESCE(SUM(score),0) FROM v_daily_entity WHERE scope='inclusive'")[0][0]
    lines.append(f"  undated share of all-time score: {100 * undated / total if total else 0:.0f}%")
    lines.append(f"  coverage backlog {backlog / MIB:.1f} MiB in {srcs} sources; tracker last pass "
                 f"{(last[0][0] if last else 0) / 1000:.1f}s, {last[0][1] if last else 0} errors; flags {len(flags(db, nm))}")
    lines.append(f"  panel: {panel}")
    lines.extend(cli_summary_lines(db))
    lines.extend(budgets.report_lines(db))
    return "\n".join(lines)


def dashboard_trends(db, today=None) -> dict:
    """Weekly CLI spend, per-bot score change, sparkline days, and freshness."""
    return trends.load_dashboard_trends(db, today)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="quota sqlite ($QUOTA_DB, else under the data root or state dir)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="window start (YYYY-MM-DD)")
    ap.add_argument("--end", type=date.fromisoformat, default=None, help="window end (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    if args.start and args.end and args.start > args.end:
        ap.error("--start is after --end")
    db_path = args.db or paths.default_db()
    if not os.path.exists(db_path):
        print(f"ERROR no database at {db_path}", file=sys.stderr)
        return 2
    db = sqlite3.connect(db_path)
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(db_path)), "panel.html")
    window = report_window(db, args.start, args.end)
    write_panel(db, out, window)
    print(summary(db, out, window))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Human-friendly, self-contained dashboard for the quota activity proxies.

Order of the page: will the week breach a budget, who is spending (bot, then
task and routine), anything abnormal, and whether the data is fresh.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quota import anonymize, budgets, leakguard, paths, report, trends
from quota.alerts import build_alerts, classify_spend
from quota.plugins import load_config
from quota.prices import effective_cost

COLORS = dict(black='#111', green='#52b56b', blue='#3b82f6', red='#e5534b',
              brown='#9a6a3a', violet='#8b5cf6', yellow='#f0a030', gray='#9a9a9a', cyan='#4fb3a3')
SHAPES = {
    'blob': 'M9 9C16 2 31 3 36 13C43 24 35 38 23 38C9 40 1 30 5 19Z',
    'squircle': 'M13 4H29Q38 4 38 13V29Q38 38 29 38H13Q4 38 4 29V13Q4 4 13 4Z',
    'wedge': 'M7 33Q2 31 6 25L20 6Q23 2 27 8L38 31Q40 37 33 37H12Q8 37 7 33Z',
    'hex': 'M13 4Q21 1 29 5L38 17Q40 21 37 27L29 37H13L4 25Q2 21 5 16Z',
    'pebble': 'M5 17Q8 4 24 5Q36 6 38 22Q39 35 23 38Q6 39 4 27Z',
    'gem': 'M12 5H30L39 18L26 37Q21 42 16 36L3 18Z',
    'shield': 'M6 6Q21 0 36 6V22Q35 31 21 39Q7 31 6 22Z',
    'house': 'M4 19Q2 17 6 14L18 4Q21 2 24 5L37 15Q40 18 37 20V33Q37 38 32 38H10Q5 38 5 33Z',
}
E = html.escape
VISIBLE = 10
MAX_AVATAR_PNG = 60 * 1024  # larger files fall back to the shape; each PNG is embedded once
STALE_MS = 26 * 60 * 60 * 1000
TOOL_ORDER = ('claude', 'codex', 'grok')
TOOL_FILL = {'claude': COLORS['yellow'], 'codex': '#6f6f6f', 'grok': COLORS['blue']}


def number(value):
    return f'{value:,.0f}'


def money(value):
    if value is None:
        return '–'
    return f'${value:,.2f}'


def fig(text) -> str:
    """Monospace tabular figure. ``text`` is already safe to embed."""
    return f'<span class="fig">{text}</span>'


def nfig(value) -> str:
    return fig(number(value))


def mfig(value) -> str:
    return fig(money(value))


def _count(value) -> str:
    return fig(number(int(value or 0)))


# Figures in prose. Bare identifiers (bot-2, job-11) are not figures.
_FIG_RE = re.compile(
    r'(?<!\w)(?:'
    r'\$\d{1,3}(?:,\d{3})*(?:\.\d+)?'
    r'|[+-]?\d+(?:\.\d+)?%'
    r'|\d+(?:\.\d+)?×'
    r'|\d{1,2}/\d{1,2}(?: \d{1,2}:\d{2})?'
    r'|\d{1,2}:\d{2}'
    r'|\d{1,3}(?:,\d{3})*(?=x\b)'
    r'|\d{1,3}(?:,\d{3})*(?= h ago)'
    r'|\d{1,3}(?:,\d{3})*(?= runs?\b)'
    r'|\d{1,3}(?:,\d{3})*(?= failed\b)'
    r')'
)


def mark_figs(text: str) -> str:
    """Escape prose, then wrap figure tokens in ``.fig``."""
    return _FIG_RE.sub(lambda match: fig(match.group(0)), E(text))


def _money_stack(cost, estimated) -> str:
    est = '<span class="est">(estimated)</span>' if estimated else ''
    return f'<span class="val">{mfig(cost)}{est}</span>'


def _profile(entity, agents=None):
    if agents is None:
        agents = paths.agents_dir()
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', entity or ''):
        return {}
    try:
        data = json.loads((Path(agents) / entity / 'profile.json').read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _name(entity, nm, agents=None):
    if agents is None:
        agents = paths.agents_dir()
    stored = nm.get(entity)
    if stored and not stored.startswith('unnamed ') and stored != (entity or '')[:8]:
        return stored
    raw = _profile(entity, agents).get('name')
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return stored or 'Others'


def _bot_label(bot, nm, agents) -> str:
    if not bot or bot == 'unattributed':
        return 'Unattributed'
    if re.fullmatch(r'[0-9a-fA-F-]{36}', bot):
        return _name(bot, nm, agents)
    return nm.get(bot) or bot


def _rest(n, inner):
    if not n:
        return ''
    return f'<details><summary>others ({fig(int(n))})</summary>{inner}</details>'


class Avatars:
    """Profile PNG bytes are embedded once, as a CSS background. The body of profile.json is not."""

    def __init__(self, agents=None):
        self.agents = paths.agents_dir() if agents is None else agents
        self.rules = []
        self.seen = set()

    def css(self) -> str:
        return ''.join(self.rules)

    def icon(self, entity, kind='bot'):
        if kind != 'bot' or not re.fullmatch(r'[0-9a-fA-F-]{36}', entity or ''):
            return ('<svg class="avatar" viewBox="0 0 42 42" aria-hidden="true">'
                    '<circle cx="21" cy="21" r="8" fill="#9a9a9a"/></svg>', COLORS['gray'])
        profile = _profile(entity, self.agents)
        color = COLORS.get(profile.get('avatarColor'), COLORS['gray'])
        png = b''
        try:
            png = (Path(self.agents) / entity / 'avatar.png').read_bytes()
        except OSError:
            png = b''
        if png and len(png) <= MAX_AVATAR_PNG:
            # Last 8 hex digits. Fixture ids share a prefix, so the prefix is not a unique key.
            token = entity.replace('-', '')[-8:].lower()
            if entity not in self.seen:
                self.seen.add(entity)
                uri = base64.b64encode(png).decode('ascii')
                self.rules.append(f'.av-{token}{{background-image:url("data:image/png;base64,{uri}")}}')
            return f'<span class="avatar photo av-{token}"></span>', color
        shape = SHAPES.get(profile.get('avatarShape'), SHAPES['blob'])
        return (f'<svg class="avatar" viewBox="0 0 42 42" aria-hidden="true"><path d="{shape}" fill="{color}"/>'
                '<ellipse cx="16" cy="21" rx="2.3" ry="3.3" fill="white"/>'
                '<ellipse cx="26" cy="21" rx="2.3" ry="3.3" fill="white"/></svg>', color)


def schedule_text(schedule):
    parts = (schedule or '').split()
    if len(parts) == 5:
        minute, hour, dom, month, dow = parts
        if dom == month == '*' and re.fullmatch(r'\d+(,\d+)*', minute) and re.fullmatch(r'\d+(,\d+)*', hour):
            count = len(minute.split(',')) * len(hour.split(','))
            period = {'*': 'every day', '1-5': 'on weekdays', '1,2,3,4,5': 'on weekdays',
                      '0,6': 'on weekends', '6,0': 'on weekends'}.get(dow)
            if period:
                return f'{count}x a day {period}'
            weekdays = {'0': 'Sunday', '1': 'Monday', '2': 'Tuesday',
                        '3': 'Wednesday', '4': 'Thursday', '5': 'Friday',
                        '6': 'Saturday', '7': 'Sunday'}
            if dow in weekdays:
                return f'{count}x a week, {weekdays[dow]}'
        if re.fullmatch(r'\d+', minute) and re.fullmatch(r'\d+', hour) and dow == '*':
            if dom.isdigit() and month == '*':
                return f'every month, on day {int(dom)}, at {int(hour):02d}:{int(minute):02d}'
            if dom.isdigit() and month.isdigit():
                return f'every year, on {int(dom):02d}/{int(month):02d}, at {int(hour):02d}:{int(minute):02d}'
    return f'cron {schedule}' if schedule else 'no schedule set'


def _legend(entities, nm, identities, faces):
    agents = faces.agents
    parts = ['<div class="legend">']
    for entity in entities:
        icon, _color = identities.get(entity) or faces.icon(entity, 'bot')
        parts.append(f'<span>{icon}{E(_name(entity, nm, agents))}</span>')
    parts.append('</div>')
    return ''.join(parts)


def chart(db, start, end, nm, identities, faces):
    agents = faces.agents
    rows = report.window_top(db, start, end, 1000000)
    groups = [r[0] for r in rows]
    daily = defaultdict(lambda: defaultdict(float))
    for day, entity, score in report.q(db, "SELECT day, entity_id, score FROM v_daily_entity WHERE scope='inclusive' AND day BETWEEN ? AND ? AND score>0", (str(start), str(end))):
        daily[day][entity] += score
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    peak = max((sum(daily[str(d)].values()) for d in days), default=0) or 1
    width, left, baseline, height = 960, 84, 198, 136
    # Extra viewBox past the plot so a large mobile font (see the 700px
    # rule) can draw the peak and the last date without clipping.
    view_w, view_h = 984, 256
    step = (width - left - 16) / len(days)
    svg = [f'<svg class="chart" viewBox="0 0 {view_w} {view_h}" role="img" aria-label="Daily activity by bot, in approximate points">'
           f'<text class="fig" x="{left - 10}" y="36" text-anchor="end">{number(peak)}</text>'
           f'<path d="M{left} {baseline}H948" stroke="#e4e4e4"/>']
    for i, day in enumerate(days):
        y = baseline
        for entity in groups:
            score = daily[str(day)][entity]
            if not score:
                continue
            h = height * score / peak
            y -= h
            color = identities.get(entity, ('', COLORS['gray']))[1]
            name = _name(entity, nm, agents)
            svg.append(f'<rect x="{left+i*step+2:.2f}" y="{y:.2f}" width="{max(step-4, .5):.2f}" height="{h:.2f}" fill="{color}"><title>{day:%d/%m} · {E(name)} · {number(score)} points</title></rect>')
        stride = max(1, len(days) // 6)
        if i % stride == 0 or i == len(days)-1:
            svg.append(f'<text class="fig" x="{left+(i+.5)*step:.2f}" y="{baseline + 40}" text-anchor="middle">{day:%d/%m}</text>')
    svg.append('</svg>' + _legend(groups[:VISIBLE], nm, identities, faces))
    extra = groups[VISIBLE:]
    if extra:
        svg.append(_rest(len(extra), _legend(extra, nm, identities, faces)))
    if not rows:
        svg.append('<p class="muted">No activity recorded in this period.</p>')
    return ''.join(svg)


CSS = '''*{box-sizing:border-box}
body{margin:0;background:#fafafa;color:#252525;font:13px/1.5 system-ui,sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 24px 48px}
h1{font-size:24px;font-weight:600;letter-spacing:-0.015em;margin:0 0 2px}
h2{font-size:17px;font-weight:600;letter-spacing:-0.01em;margin:36px 0 8px}
h1+h2{margin-top:20px}
h3{font-size:14px;font-weight:600;margin:16px 0 4px}
p{margin:4px 0 8px}
.muted,footer{color:#6f6f6f}
.aside{color:#6f6f6f}
.fig{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.kpi{margin:2px 0 2px;color:#6f6f6f}
.kpi strong{color:#252525;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums;font-size:18px;font-weight:600;letter-spacing:-0.02em}
.muted .fig{color:#444}
.status{max-width:1040px;margin:0 auto;padding:16px 24px 14px;border-bottom:1px solid #e7e7e7}
.fresh,.stale{display:flex;flex-wrap:wrap;align-items:baseline;column-gap:8px;row-gap:2px;margin:0;font-size:12px;line-height:1.45}
.fresh{color:#666}
.stale{background:#fde8e6;color:#e5534b;border:1px solid #f5c2c0;padding:10px 12px;font-weight:600}
.fact{white-space:nowrap}
.fact-state{font-weight:600}
.sep{color:#c8c8c8}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:12px;margin:12px 0 0;padding:0;list-style:none}
.kpi-card{display:flex;flex-direction:column;margin:0;min-width:0;padding:10px 12px 11px;background:#fff;border:1px solid #e4e4e4;border-left:3px solid #c8c8c8}
.kpi-card.critical{border-left-color:#e5534b}
.kpi-card.warning{border-left-color:#e0a040}
.kpi-card.ok{border-left-color:#c8c8c8}
.kpi-k{margin:0;color:#6f6f6f;font-size:11px;font-weight:600;line-height:1.3}
.kpi-v{margin:2px 0 0;color:#252525;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums;font-size:22px;font-weight:600;letter-spacing:-0.03em;line-height:1.2;overflow-wrap:anywhere}
.kpi-v-long{font-size:15px;line-height:1.25}
.kpi-card.critical .kpi-v{color:#e5534b}
.kpi-card.warning .kpi-v{color:#8a5a10}
.kpi-s{margin:0;padding-top:8px;color:#6f6f6f;font-size:12px;line-height:1.35;overflow-wrap:anywhere}
.kpi-a{margin:2px 0 0;color:#444;font-size:12px;line-height:1.35;overflow-wrap:anywhere}
.kpi-s .fig,.kpi-a .fig{color:#333}
.identity{display:flex;align-items:center;gap:8px}
.avatar{width:30px;height:30px;flex-shrink:0}
.photo{border-radius:50%;background-size:cover;background-position:center;background-repeat:no-repeat}
span.avatar{display:inline-block;vertical-align:middle}
.hint{display:block;margin-left:38px;color:#9a5b12;font-size:12px;font-weight:500;margin-top:1px}
.tool{display:grid;grid-template-columns:minmax(9rem,11rem) 6.5rem minmax(72px,1fr) 6.75rem minmax(9.5rem,12rem);column-gap:14px;align-items:center;padding:8px 0}
.tool-name{font-weight:600;line-height:1.25}
.tool-name .est{display:block;margin-top:1px;color:#8d8d8d;font-size:11px;font-weight:400;line-height:1.25}
.tool-amt{text-align:right}
.tool-amt .fig{font-size:14px;font-weight:600}
.meter{height:4px;background:#e0e0e0;display:block;overflow:hidden}
.meter>span{display:block;height:100%;background:#3b82f6}
.meter>span.near{background:#f0a030}
.meter>span.over{background:#e5534b}
.tool-of{text-align:right;color:#6f6f6f;white-space:nowrap}
.tool-of .fig{color:#252525}
.tool-trend{text-align:left;white-space:nowrap;color:#6f6f6f}
.tool.total{margin-top:4px;padding-top:12px;border-top:1px solid #e7e7e7}
.vs{color:#6f6f6f}
.trend-pct{display:inline-block;min-width:4.6rem;text-align:right}
.trend-pct.new{font-family:system-ui,sans-serif;color:#6f6f6f;font-weight:500}
.over{color:#e5534b;font-weight:700}
.near{color:#8a5a10;font-weight:700}
.ok{color:#252525}
.todo-state{color:#9a5b12;font-weight:600}
.todo td{background:#fffaf3}
.spark{vertical-align:middle;flex:none}
.trend{display:inline-flex;align-items:center;gap:8px;white-space:nowrap}
table.bots,table.cli{border-collapse:collapse;width:100%;margin:8px 0 4px}
table.bots th,table.bots td,table.cli th,table.cli td{border-bottom:1px solid #eee;padding:9px 10px;text-align:left;vertical-align:middle}
table.bots th:first-child,table.bots td:first-child,table.cli th:first-child,table.cli td:first-child{padding-left:0}
table.bots th:last-child,table.bots td:last-child,table.cli th:last-child,table.cli td:last-child{padding-right:0}
table.bots th,table.cli th{color:#6f6f6f;font-weight:600;font-size:12px;border-bottom-color:#e4e4e4;padding-top:2px;padding-bottom:8px;white-space:nowrap}
th.num,td.num{text-align:right}
td.num{white-space:nowrap}
td.cost .val{display:inline-block;text-align:right}
td.cost .est{display:block;margin-top:2px;color:#8d8d8d;font-size:11px;font-weight:400;font-family:system-ui,sans-serif;line-height:1.2}
table.bots tbody tr:hover td,table.cli tbody tr:hover td{background:#f4f4f4}
tr.todo:hover td{background:#fff3e4}
.wide{overflow-x:auto}
.chart{width:100%;height:auto;display:block;margin-top:8px}
.chart text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;font-variant-numeric:tabular-nums;fill:#6f6f6f}
.legend{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:12px;color:#6f6f6f;margin-top:12px}
.legend span{display:flex;align-items:center;gap:6px}
.legend .avatar{width:22px;height:22px}
ul{list-style:none;padding:0;margin:8px 0 0}
li{margin:8px 0;line-height:1.45}
footer{border-top:1px solid #e7e7e7;margin-top:36px;padding-top:14px}
.cov{margin:0;font-size:11px;line-height:1.65;color:#8a8a8a;display:flex;flex-wrap:wrap;align-items:baseline;column-gap:18px;row-gap:3px}
.cov-label{color:#6f6f6f;font-weight:600}
.cov-item{white-space:nowrap}
.cov .fig{color:#444}
details{margin-top:12px}
details.fold{margin-top:28px}
details.fold>summary{cursor:pointer;font-weight:600;font-size:17px;color:#252525}
details>summary{cursor:pointer;color:#6f6f6f}
details .legend{margin-top:12px}
@media(max-width:700px){
main{padding:20px 16px 36px}
.status{padding:14px 16px 12px}
.fresh,.stale{flex-direction:column;align-items:flex-start;row-gap:3px}
.fresh .sep,.stale .sep{display:none}
.fact{white-space:normal}
.chart text{font-size:30px}
.cov{flex-direction:column;align-items:stretch;row-gap:3px}
.cov-item{white-space:normal;min-width:0}
.tool{grid-template-columns:1fr auto;grid-template-areas:"name amt" "bar bar" "of trend";column-gap:12px;row-gap:4px;padding:11px 0}
.tool-name{grid-area:name}
.tool-amt{grid-area:amt;justify-self:end}
.meter,.tool-gap{grid-area:bar}
.meter{height:6px;margin:5px 0}
.tool-of{grid-area:of;text-align:left}
.tool-trend{grid-area:trend;justify-self:end}
.tool.total{grid-template-areas:"name amt" "trend trend"}
.tool.total .tool-gap,.tool.total .tool-of{display:none}
.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
.wide{overflow:visible}
table.bots thead,table.cli thead{display:none}
table.bots,table.cli,table.bots tbody,table.cli tbody,table.bots tr,table.cli tr{display:block}
table.bots tr,table.cli tr{padding:10px 0;border-bottom:1px solid #eee}
table.bots tr{padding:12px 0}
table.bots td,table.cli td{display:flex;justify-content:space-between;align-items:center;gap:12px;border:0;padding:3px 0;text-align:left}
table.bots td:first-child,table.cli td:first-child{display:block;padding-bottom:4px}
table.bots td:nth-child(2)::before{content:"CLI cost (USD)"}
table.bots td:nth-child(3)::before{content:"activity points"}
table.bots td:nth-child(4)::before{content:"trend"}
table.bots td:nth-child(5)::before{content:"budget"}
table.cli td:nth-child(2)::before{content:"tool"}
table.cli td:nth-child(3)::before{content:"runs"}
table.cli td:nth-child(4)::before{content:"cost (USD)"}
table.bots td::before,table.cli td::before{color:#6f6f6f;font-size:12px;font-weight:600}
.trend,.num{white-space:normal}
.trend{flex-wrap:wrap;justify-content:flex-end}
td.num{text-align:right}
td.cost .est:empty{display:none}
}
@media(max-width:390px){
.kpis{grid-template-columns:minmax(0,1fr)}
}
'''


def _moment(now):
    if now is None:
        return datetime.now(budgets.TZ)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=budgets.TZ)
        return now.astimezone(budgets.TZ)
    if isinstance(now, date):
        return datetime(now.year, now.month, now.day, 12, tzinfo=budgets.TZ)
    return datetime.now(budgets.TZ)


def _stamp(ms) -> str:
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone(budgets.TZ)
    return dt.strftime('%d/%m %H:%M')


def freshness_view(freshness, moment):
    """Status facts plus the stale phrases that become one alert.

    A source is stale when it is older than 26 hours. Missing timestamps are
    shown as ``none`` and are not stale. Returns ``(facts, stale_parts, state)``.
    """
    now_ms = int(moment.timestamp() * 1000)
    display = []
    stale_parts = []
    specs = (
        ('last collect', 'last_collect_ms', 'Last collect'),
        ('last search-index message', 'max_search_index_message_ms', 'Newest search-index message'),
    )
    for label, key, nice in specs:
        ms = None if not freshness else freshness.get(key)
        if ms is None:
            display.append(f'{nice}: none')
            continue
        age = now_ms - int(ms)
        hours = age // (60 * 60 * 1000)
        display.append(f'{nice}: {_stamp(ms)} ({max(hours, 0)} h ago)')
        if age > STALE_MS:
            stale_parts.append(f'{label} {hours} h ago ({_stamp(ms)})')
    state = 'stale' if stale_parts else 'OK'
    return display, stale_parts, state


def _trend_bits(this, prev) -> str:
    label = trends.format_change(trends.change_pct(float(this or 0), float(prev or 0)))
    if label is None:
        return '<span class="trend-pct new">new</span>'
    return f'<span class="fig trend-pct">{E(label)}</span><span class="vs"> vs last week</span>'


def sparkline(values) -> str:
    vals = [float(v or 0) for v in (values or [])] or [0.0]
    if len(vals) == 1:
        vals = [vals[0], vals[0]]
    width, height, pad = 72, 16, 1.5
    peak = max(vals) or 1.0
    last = len(vals) - 1
    points = []
    for i, value in enumerate(vals):
        x = pad + (width - 2 * pad) * (i / last)
        y = pad + (height - 2 * pad) * (1 - value / peak)
        points.append(f'{x:.1f},{y:.1f}')
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'aria-hidden="true"><polyline points="{" ".join(points)}" fill="none" stroke="#3b82f6" '
            f'stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/></svg>')


def _trend_html(row) -> str:
    daily = row['daily'] if row else [0.0] * 7
    if row is None or row.get('change_label') is None:
        bits = '<span class="trend-pct new">new</span>'
    else:
        bits = f'<span class="fig trend-pct">{E(row["change_label"])}</span><span class="vs"> vs last week</span>'
    return f'<span class="trend">{sparkline(daily)}{bits}</span>'


def load_cli_rows(db) -> list[dict]:
    """Effective CLI rows. Missing estimate or attribution columns count as absent."""
    if not report.q(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cli_runs'"):
        return []
    cols = {row[1] for row in report.q(db, 'PRAGMA table_info(cli_runs)')}
    est = 'cost_usd_est' if 'cost_usd_est' in cols else 'NULL'
    flag = 'COALESCE(cost_estimated,0)' if 'cost_estimated' in cols else '0'
    method = 'attribution_method' if 'attribution_method' in cols else 'NULL'
    task = 'task' if 'task' in cols else 'NULL'
    sql = f'SELECT tool, {task}, bot_id, ts, cost_usd, {est}, {flag}, {method} FROM cli_runs'
    out = []
    for tool, task_v, bot, ts, cost_usd, cost_est, flag_v, method_v in report.q(db, sql):
        cost = effective_cost(cost_usd, cost_est)
        day = budgets.lisbon_day_of(ts)
        if cost is None or day is None or not tool:
            continue
        out.append({
            'tool': tool,
            'task': task_v or None,
            'bot_id': bot or 'unattributed',
            'day': day,
            'cost': float(cost),
            'estimated': cost_usd is None and bool(flag_v),
            'method': method_v or 'unattributed',
        })
    return out


def _slot():
    return {'daily': 0.0, 'weekly': 0.0, 'daily_est': False, 'weekly_est': False}


def _add_bucket(buckets, key, row, today):
    bucket = buckets.setdefault(key, _slot())
    cost = float(row.get('cost') or 0)
    estimated = bool(row.get('estimated')) and cost != 0
    bucket['weekly'] += cost
    bucket['weekly_est'] = bucket['weekly_est'] or estimated
    if row.get('day') == today:
        bucket['daily'] += cost
        bucket['daily_est'] = bucket['daily_est'] or estimated
    return bucket


def _in_this_week(day, today) -> bool:
    start, end, _prev_start, _prev_end = budgets.week_bounds(today)
    return start <= day <= end


def _routine_hint(db, owner) -> str | None:
    rows = report.q(db, 'SELECT routine_id, name, enabled FROM routines WHERE owner_id=?', (owner,))
    best = None
    for rid, name, enabled in rows:
        runs = report.q(
            db, 'SELECT COUNT(*) FROM runs WHERE owner_id=? AND routine_id=?', (owner, rid))[0][0]
        label = name or rid
        key = (1 if enabled else 0, int(runs or 0), label)
        if best is None or key > best[0]:
            best = (key, label)
    return None if best is None else best[1]


def _paused_tool_names(cfg) -> list[str]:
    names = set()
    quota_cfg = load_config()
    for key in ('paused_tools', 'disabled_tools'):
        raw = quota_cfg.get(key) if isinstance(quota_cfg, dict) else None
        if isinstance(raw, list):
            names.update(item.strip() for item in raw if isinstance(item, str) and item.strip())
    tools = ((cfg or {}).get('overrides') or {}).get('tools') or {}
    if isinstance(tools, dict):
        for name, extra in tools.items():
            if isinstance(extra, dict) and (extra.get('paused') is True or extra.get('enabled') is False):
                names.add(str(name))
    return sorted(names)


def _budget_items(db, cli_rows, today, cfg, nm, agents) -> list[dict]:
    if not cfg:
        return []
    tools = {name: _slot() for name in TOOL_ORDER}
    for name in ((cfg.get('overrides') or {}).get('tools') or {}):
        tools.setdefault(name, _slot())
    bots = {}
    for row in cli_rows:
        if not _in_this_week(row['day'], today) or not row.get('tool'):
            continue
        _add_bucket(tools, row['tool'], row, today)
        bot = row.get('bot_id') or 'unattributed'
        if bot != 'unattributed':
            _add_bucket(bots, bot, row, today)
    for name in ((cfg.get('overrides') or {}).get('bots') or {}):
        if name != 'unattributed':
            bots.setdefault(name, _slot())
    items = []

    def emit(kind, name, bucket):
        limits = budgets.limits_for(cfg, kind, name)
        routine = _routine_hint(db, name) if kind == 'bot' else None
        label = name if kind == 'tool' else _bot_label(name, nm, agents)
        for window, spend_key, est_key, limit_key in (
            ('daily', 'daily', 'daily_est', 'daily_usd'),
            ('weekly', 'weekly', 'weekly_est', 'weekly_usd'),
        ):
            items.append({
                'kind': kind, 'name': name, 'label': label, 'window': window,
                'spend': bucket[spend_key], 'limit': limits.get(limit_key),
                'estimated': bucket[est_key], 'routine': routine,
            })

    for name in [n for n in TOOL_ORDER if n in tools] + sorted(n for n in tools if n not in TOOL_ORDER):
        emit('tool', name, tools[name])
    for name in sorted(bots):
        emit('bot', name, bots[name])
    return items


def _paused_in_use(db, cli_rows, today, cfg, nm, agents) -> list[dict]:
    found = []
    spend = defaultdict(float)
    for row in cli_rows:
        if _in_this_week(row['day'], today) and row.get('tool'):
            spend[row['tool']] += float(row['cost'] or 0)
    for name in _paused_tool_names(cfg):
        if spend.get(name, 0) > 0:
            found.append({'kind': 'tool', 'name': name})
    start, end, _prev_start, _prev_end = budgets.week_bounds(today)
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=budgets.TZ).timestamp() * 1000)
    end_ms = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=budgets.TZ).timestamp() * 1000)
    rows = report.q(
        db,
        'SELECT r.owner_id, r.routine_id, r.name FROM routines r '
        'JOIN runs u ON u.owner_id=r.owner_id AND u.routine_id=r.routine_id '
        'WHERE COALESCE(r.enabled, 1)=0 AND u.started_at_ms BETWEEN ? AND ? '
        'GROUP BY r.owner_id, r.routine_id, r.name '
        'ORDER BY r.name, r.routine_id',
        (start_ms, end_ms))
    for owner, rid, name in rows:
        label = name or rid
        owner_label = _bot_label(owner, nm, agents) if owner else ''
        shown = f'{owner_label} / {label}' if owner_label else label
        found.append({'kind': 'routine', 'name': shown})
    return found


def _spike_numbers(cli_rows, today) -> dict:
    this_start, this_end, prev_start, prev_end = budgets.week_bounds(today)
    daily = defaultdict(float)
    tasks_today = defaultdict(float)
    tasks_week = defaultdict(float)
    week_spend = prev_spend = 0.0
    for row in cli_rows:
        day = row['day']
        cost = float(row['cost'] or 0)
        daily[day] += cost
        task = row.get('task')
        if day == today:
            tasks_today[task] += cost
        if this_start <= day <= this_end:
            week_spend += cost
            tasks_week[task] += cost
        elif prev_start <= day <= prev_end:
            prev_spend += cost

    def top(bucket):
        if not bucket:
            return None
        winner = sorted(bucket, key=lambda key: (-bucket[key], str(key or '')))[0]
        return winner or None

    trailing = [today - timedelta(days=i) for i in range(1, 8)]
    return {
        'today_spend': daily[today],
        'trailing_daily_avg': sum(daily[day] for day in trailing) / 7.0,
        'week_spend': week_spend,
        'trailing_week_spend': prev_spend,
        'top_task_today': top(tasks_today),
        'top_task_week': top(tasks_week),
    }


def _unattributed_share(cli_rows, today):
    total = unattr = 0.0
    for row in cli_rows:
        if not _in_this_week(row['day'], today):
            continue
        cost = float(row['cost'] or 0)
        total += cost
        if (row.get('bot_id') or 'unattributed') == 'unattributed':
            unattr += cost
    if total <= 0:
        return None
    return unattr / total


def collect_alerts(db, today, moment, fresh, cli_rows, cfg, nm, agents) -> list[dict]:
    _facts, stale_parts, _state = freshness_view(fresh.get('freshness'), moment)
    snapshot = {
        'budgets': _budget_items(db, cli_rows, today, cfg, nm, agents),
        'paused_in_use': _paused_in_use(db, cli_rows, today, cfg, nm, agents),
        'unattributed_share': _unattributed_share(cli_rows, today),
        'stale_parts': stale_parts,
    }
    snapshot.update(_spike_numbers(cli_rows, today))
    return build_alerts(snapshot)


def page_alerts(db, today=None, moment=None, agents=None) -> list[dict]:
    """Alerts for a database. ``today`` defaults to ``moment``, then Lisbon now."""
    if agents is None:
        agents = paths.agents_dir()
    if moment is None:
        moment = today
    moment = _moment(moment)
    if today is None:
        today = moment.date()
    elif isinstance(today, datetime):
        today = budgets.lisbon_today(today)
    fresh = report.dashboard_trends(db, today)
    nm = report.names(db)
    for eid in list(nm):
        nm[eid] = _name(eid, nm, agents)
    cli_rows = load_cli_rows(db)
    try:
        cfg = budgets.load_budgets(budgets.DEFAULT_BUDGETS_PATH)
    except (OSError, ValueError):
        cfg = None
    return collect_alerts(db, today, moment, fresh, cli_rows, cfg, nm, agents)


_TONE = {'high': 'critical', 'medium': 'warning', 'ok': 'ok'}


def _hours_between(ms, moment) -> int:
    now_ms = int(moment.timestamp() * 1000)
    return max((now_ms - int(ms)) // (60 * 60 * 1000), 0)


def _kpi_card(tone, label, value, detail='', action='') -> str:
    lines = []
    if detail:
        lines.append(f'<p class="kpi-s">{mark_figs(detail)}</p>')
    if action:
        lines.append(f'<p class="kpi-a">{mark_figs(action)}</p>')
    long = ' kpi-v-long' if len(str(value)) > 16 else ''
    return (
        f'<li class="kpi-card {E(tone)}">'
        f'<p class="kpi-k">{E(label)}</p>'
        f'<p class="kpi-v{long}">{fig(E(str(value)))}</p>'
        f'{"".join(lines)}</li>')


def _alert_card(item) -> str:
    tone = _TONE.get(item.get('severity'), 'ok')
    return _kpi_card(
        tone,
        item.get('label') or item.get('kind') or 'Alert',
        item.get('value') if item.get('value') not in (None, '') else item.get('kind') or '',
        item.get('detail') or '',
        item.get('action') or '')


def _metric_cards(fresh, moment) -> list[dict]:
    """Week spend, activity, and collect age when nothing is alerting."""
    fresh = fresh or {}
    rows = fresh.get('cli_by_tool') or []
    total = sum(float(row.get('this_usd') or 0) for row in rows)
    prev = sum(float(row.get('prev_usd') or 0) for row in rows)
    estimated = any(row.get('this_estimated') for row in rows)
    change = trends.format_change(trends.change_pct(total, prev))
    spend_detail = 'last 7 days'
    if estimated:
        spend_detail += ' · includes estimated'
    spend_action = f'{change} vs last week' if change is not None else 'new'
    bots = fresh.get('bots') or []
    score = sum(float(row.get('this_score') or 0) for row in bots)
    prev_score = sum(float(row.get('prev_score') or 0) for row in bots)
    score_change = trends.format_change(trends.change_pct(score, prev_score))
    score_action = f'{score_change} vs last week' if score_change is not None else 'new'
    freshness = fresh.get('freshness') or {}
    collect_ms = freshness.get('last_collect_ms')
    message_ms = freshness.get('max_search_index_message_ms')
    if collect_ms is None:
        collect_value = 'none'
        collect_detail = 'no collector run'
    else:
        collect_value = f'{_hours_between(collect_ms, moment)} h ago'
        collect_detail = _stamp(collect_ms)
    if message_ms is None:
        collect_action = 'newest message none'
    else:
        collect_action = (
            f'newest message {_stamp(message_ms)} ({_hours_between(message_ms, moment)} h ago)')
    return [
        {'tone': 'ok', 'label': 'Week spend', 'value': money(total),
         'detail': spend_detail, 'action': spend_action},
        {'tone': 'ok', 'label': 'Activity', 'value': number(score),
         'detail': 'points this week · not a quota', 'action': score_action},
        {'tone': 'ok', 'label': 'Last collect', 'value': collect_value,
         'detail': collect_detail, 'action': collect_action},
    ]


def _status_html(freshness, moment, items, fresh=None) -> str:
    facts, _stale_parts, state = freshness_view(freshness, moment)
    klass = 'stale' if state == 'stale' else 'fresh'
    sep = '<span class="sep" aria-hidden="true">·</span>'
    bits = [f'<span class="fact">{mark_figs(part)}</span>' for part in facts]
    bits.append(f'<span class="fact fact-state">{E(state)}</span>')
    if items:
        cards = ''.join(_alert_card(item) for item in items)
        caption = 'Alerts'
    else:
        cards = ''.join(_kpi_card(**card) for card in _metric_cards(fresh, moment))
        caption = 'Key figures'
    return (
        f'<div class="status"><p class="{klass}">{sep.join(bits)}</p>'
        f'<ul class="kpis" aria-label="{caption}">{cards}</ul></div>')


def _budget_status(daily, weekly, limits) -> str:
    levels = (
        classify_spend(daily, limits.get('daily_usd') if limits else None),
        classify_spend(weekly, limits.get('weekly_usd') if limits else None),
    )
    if 'over' in levels:
        return 'over'
    if 'near' in levels:
        return 'near'
    return 'ok'


def _bot_records(today, fresh, cli_rows, cfg, nm, agents) -> list[dict]:
    trend_by = {row['entity_id']: row for row in fresh['bots']}
    spent = {}
    for row in cli_rows:
        if not _in_this_week(row['day'], today):
            continue
        bot = row.get('bot_id') or 'unattributed'
        bucket = spent.setdefault(bot, {'cost': 0.0, 'estimated': False, 'daily': 0.0})
        cost = float(row['cost'] or 0)
        bucket['cost'] += cost
        bucket['estimated'] = bucket['estimated'] or (bool(row.get('estimated')) and cost != 0)
        if row['day'] == today:
            bucket['daily'] += cost
    records = []
    for eid, trend in trend_by.items():
        bucket = spent.get(eid) or {'cost': 0.0, 'estimated': False, 'daily': 0.0}
        if trend['this_score'] == 0 and trend['prev_score'] == 0 and bucket['cost'] == 0:
            continue
        limits = budgets.limits_for(cfg, 'bot', eid) if cfg else {}
        records.append({
            'id': eid,
            'label': _bot_label(eid, nm, agents),
            'cost': bucket['cost'],
            'estimated': bucket['estimated'],
            'points': float(trend['this_score']),
            'trend': _trend_html(trend),
            'status': _budget_status(bucket['daily'], bucket['cost'], limits),
            'unattributed': False,
        })
    for eid, bucket in spent.items():
        if eid == 'unattributed' or eid in trend_by or bucket['cost'] == 0:
            continue
        limits = budgets.limits_for(cfg, 'bot', eid) if cfg else {}
        records.append({
            'id': eid,
            'label': _bot_label(eid, nm, agents),
            'cost': bucket['cost'],
            'estimated': bucket['estimated'],
            'points': 0.0,
            'trend': _trend_html(None),
            'status': _budget_status(bucket['daily'], bucket['cost'], limits),
            'unattributed': False,
        })
    bucket = spent.get('unattributed')
    if bucket and bucket['cost'] > 0:
        records.append({
            'id': 'unattributed',
            'label': 'Unattributed',
            'cost': bucket['cost'],
            'estimated': bucket['estimated'],
            'points': 0.0,
            'trend': _trend_html(None),
            'status': 'to-do',
            'unattributed': True,
        })
    records.sort(key=lambda rec: (-rec['cost'], -rec['points'], rec['label'].casefold(), rec['id']))
    return records


def _bot_row(rec, identities, faces) -> str:
    if rec['unattributed']:
        icon, _color = faces.icon(None, 'unknown')
    else:
        icon, _color = identities.get(rec['id']) or faces.icon(rec['id'], 'bot')
    name = f'<div class="identity">{icon}<strong>{E(rec["label"])}</strong></div>'
    if rec['unattributed']:
        name += '<span class="hint">adopt grokbot-run</span>'
    if rec['status'] == 'to-do':
        state = '<td class="todo-state">to-do</td>'
    else:
        state = f'<td class="{rec["status"]}">{rec["status"]}</td>'
    klass = 'bot todo' if rec['unattributed'] else 'bot'
    plan = ''
    if rec.get('plan_pct') is not None:
        plan = f'<td class="num plan">{fig(f"{rec["plan_pct"]:.1f}%")}</td>'
    detail = rec.get('detail', '')
    if detail:
        name += f'<span class="muted activity-detail">{detail}</span>'
    return (f'<tr class="{klass}"><td>{name}</td>{plan}'
            f'<td class="num cost">{_money_stack(rec["cost"], rec["estimated"])}</td>'
            f'<td class="num">{nfig(rec["points"])}</td><td>{rec["trend"]}</td>{state}</tr>')


def _bot_table(records, identities, faces) -> str:
    body = ''.join(_bot_row(rec, identities, faces) for rec in records)
    plan_head = '<th class="num">% of plan</th>' if any(rec.get("plan_pct") is not None for rec in records) else ''
    return ('<div class="wide"><table class="bots"><thead><tr>'
            f'<th>bot</th>{plan_head}<th class="num">CLI cost (USD)</th><th class="num">activity points</th>'
            '<th>trend</th><th>budget</th>'
            f'</tr></thead><tbody>{body}</tbody></table></div>')


def spend_section(fresh, cfg) -> str:
    by = {row['tool']: row for row in fresh['cli_by_tool']}
    names = [tool for tool in TOOL_ORDER]
    names += sorted(tool for tool in by if tool not in names)
    this_total = prev_total = 0.0
    any_est = False
    rows = []
    for tool in names:
        row = by.get(tool) or {'this_usd': 0.0, 'prev_usd': 0.0, 'this_estimated': False}
        this = float(row['this_usd'])
        prev = float(row['prev_usd'])
        this_total += this
        prev_total += prev
        estimated = bool(row.get('this_estimated'))
        any_est = any_est or estimated
        limit = budgets.limits_for(cfg, 'tool', tool).get('weekly_usd') if cfg else None
        level = classify_spend(this, limit)
        if limit:
            width = 0.0 if limit == 0 else min(100.0, this / float(limit) * 100.0)
        else:
            width = 0.0 if this == 0 else 100.0
        fill = level or 'ok'
        bar = COLORS['red'] if fill == 'over' else TOOL_FILL.get(tool, COLORS['blue'])
        note = '<span class="est">(estimated)</span>' if estimated else ''
        of_limit = f'of {mfig(limit)}' if limit is not None else ''
        rows.append(
            '<div class="tool">'
            f'<span class="tool-name">{E(tool)}{note}</span>'
            f'<span class="tool-amt"><strong>{mfig(this)}</strong></span>'
            f'<span class="meter" role="img" aria-label="{E(tool)} weekly budget">'
            f'<span class="{fill}" style="width:{width:.1f}%;background:{bar}"></span></span>'
            f'<span class="tool-of">{of_limit}</span>'
            f'<span class="tool-trend">{_trend_bits(this, prev)}</span></div>')
    total_note = '<span class="est">(includes estimated)</span>' if any_est else ''
    rows.append(
        '<div class="tool total">'
        f'<span class="tool-name">Total{total_note}</span>'
        f'<span class="tool-amt"><strong>{mfig(this_total)}</strong></span>'
        '<span class="tool-gap"></span><span class="tool-of"></span>'
        f'<span class="tool-trend">{_trend_bits(this_total, prev_total)}</span></div>')
    return ('<h2>This week\'s spend</h2>'
            '<p class="muted">CLI spend this week (last 7 days).</p>'
            + ''.join(rows))


def bots_section(fresh, records, identities, faces) -> str:
    this_score = sum(row['this_score'] for row in fresh['bots'])
    prev_score = sum(row['prev_score'] for row in fresh['bots'])
    parts = ['<h2>Bots</h2>',
             f'<p class="kpi"><strong>{number(this_score)}</strong> activity points (not a money unit, not a quota)</p>',
             f'<p class="muted">{_trend_bits(this_score, prev_score)}</p>']
    if not records:
        parts.append('<p class="muted">No bot spend or activity this week.</p>')
        return ''.join(parts)
    parts.append(_bot_table(records[:VISIBLE], identities, faces))
    extra = records[VISIBLE:]
    if extra:
        parts.append(_rest(len(extra), _bot_table(extra, identities, faces)))
    return ''.join(parts)


def _col_attr(index, numeric, cost) -> str:
    classes = []
    if index in numeric:
        classes.append('num')
    if cost is not None and index == cost:
        classes.append('cost')
    if not classes:
        return ''
    return f' class="{" ".join(classes)}"'


def _html_table(headers, body, numeric=frozenset(), cost=None) -> str:
    head = ''.join(f'<th{_col_attr(i, numeric, cost)}>{h}</th>' for i, h in enumerate(headers))
    lines = []
    for row in body:
        cells = ''.join(f'<td{_col_attr(i, numeric, cost)}>{cell}</td>' for i, cell in enumerate(row))
        lines.append(f'<tr>{cells}</tr>')
    return f'<div class="tablewrap"><table class="cli"><thead><tr>{head}</tr></thead><tbody>{"".join(lines)}</tbody></table></div>'


def _capped(headers, body, numeric=frozenset(), cost=None) -> str:
    if len(body) <= VISIBLE:
        return _html_table(headers, body, numeric, cost)
    extra = body[VISIBLE:]
    return (_html_table(headers, body[:VISIBLE], numeric, cost)
            + _rest(len(extra), _html_table(headers, extra, numeric, cost)))


def _attr_line(rows) -> str | None:
    if not rows:
        return None
    counts = defaultdict(int)
    for row in rows:
        counts[row.get('method') or 'unattributed'] += 1
    order = ('launch_session', 'launch_cwd', 'plugin', 'unattributed')
    bits = [f'{name} {int(counts.get(name, 0))}' for name in order]
    return 'attribution: ' + ', '.join(bits)


def tasks_section(cli_rows, today) -> str:
    week = [row for row in cli_rows if _in_this_week(row['day'], today)]
    parts = ['<details class="fold"><summary>Tasks</summary>']
    if not week:
        parts.append('<p class="muted">No tasks this week.</p></details>')
        return ''.join(parts)
    note = _attr_line(week)
    if note:
        parts.append(f'<p class="muted">{E(note)}</p>')
    grouped = {}
    for row in week:
        key = (row.get('task') or '–', row.get('tool') or '?')
        slot = grouped.setdefault(key, {'runs': 0, 'cost': 0.0, 'estimated': False})
        slot['runs'] += 1
        slot['cost'] += float(row['cost'] or 0)
        slot['estimated'] = slot['estimated'] or (bool(row.get('estimated')) and float(row['cost'] or 0) != 0)
    ordered = sorted(grouped, key=lambda key: (-grouped[key]['cost'], key[0], key[1]))
    body = []
    for task, tool in ordered:
        slot = grouped[(task, tool)]
        body.append([
            E(task), E(tool), nfig(slot['runs']),
            _money_stack(slot['cost'], slot['estimated']),
        ])
    parts.append(_capped(
        ['task', 'tool', 'runs', 'cost (USD)'], body, numeric=frozenset({2, 3}), cost=3))
    parts.append('</details>')
    return ''.join(parts)


def routines_section(db, nm, agents) -> str:
    parts = ['<details class="fold"><summary>Routines</summary>',
             '<p class="muted">Runs across all available history.</p><ul>']
    routines = report.q(
        db, 'SELECT owner_id, routine_id, name, enabled, schedule, timezone FROM routines ORDER BY owner_id, routine_id')
    for owner, rid, name, enabled, schedule, tz in routines:
        runs, failed = report.q(
            db, "SELECT COUNT(*), COALESCE(SUM(status IN ('error','failed','failure','cancelled','canceled')),0) "
                'FROM runs WHERE owner_id=? AND routine_id=?', (owner, rid))[0]
        line = f'{_bot_label(owner, nm, agents)} · {name or rid} · {schedule_text(schedule)}'
        if tz:
            line += f' ({tz})'
        if not enabled:
            line += ' · paused'
        run_word = 'run' if runs == 1 else 'runs'
        counts = f'{number(runs)} {run_word}, {number(failed)} failed'
        parts.append(f'<li>{mark_figs(line)} · {fig(counts)}</li>')
    if not routines:
        parts.append('<li class="muted">No routines recorded.</li>')
    parts.append('</ul></details>')
    return ''.join(parts)


def legacy_section(db, nm) -> str:
    parts = ['<details class="fold"><summary>Legacy history</summary>', '<h2>Computer use</h2>']
    cu = report.q(
        db, "SELECT COALESCE(root_entity_id,entity_id,'unknown'), COUNT(*), "
            "COALESCE(SUM(duration_ms),0)/60000.0, MAX(est_at_ms) FROM v_canonical_activity "
            "WHERE event_type='computer_session' GROUP BY 1 ORDER BY 2 DESC")
    minutes = sum(r[2] for r in cu)
    last = max((r[3] or 0 for r in cu), default=0)
    stamp = report.ms_str(last)
    stamp_html = stamp if stamp == '–' else fig(E(stamp))
    parts.append(
        f'<p>{nfig(sum(r[1] for r in cu))} sessions · {fig(f"{minutes:.1f}")} minutes in total · '
        f'last session: {stamp_html}</p>')
    if cu:
        parts.append('<p>' + ' · '.join(
            f'{E(nm.get(r[0]) or _name(r[0], nm))}: {nfig(r[1])} sessions' for r in cu[:5]) + '</p>')
    parts.append('</details>')
    return ''.join(parts)


def _footer(db) -> str:
    cov = report.q(db, 'SELECT source_type, sources, ok, missing, errored FROM v_coverage ORDER BY source_type')
    if not cov:
        return '<footer><p class="cov"><span class="cov-label">Coverage</span><span class="cov-item">no sources</span></p></footer>'
    items = ''.join(
        f'<span class="cov-item">{E(t)}: {_count(n)} sources '
        f'({_count(ok)} ok, {_count(missing)} missing, {_count(err)} with errors)</span>'
        for t, n, ok, missing, err in cov)
    return f'<footer><p class="cov"><span class="cov-label">Coverage</span>{items}</p></footer>'


def plan_shares(latest_pct, rows, total):
    """Scale each bot's activity share by the latest plan usage reading."""
    return [(row[0], row[1] / total * 100 if total else 0,
             row[1] / total * float(latest_pct or 0) if total else 0) for row in rows]


def hero_section(db, fresh, records, window_rows, identities, faces, nm, agents, start, end):
    try:
        reading = db.execute(
            'SELECT day, usage_pct, recorded_at_ms FROM usage_readings '
            'WHERE usage_pct IS NOT NULL ORDER BY day DESC LIMIT 1').fetchone()
    except sqlite3.OperationalError:
        reading = None
    pct = float(reading[1]) if reading else None
    total = sum(row[1] for row in window_rows)
    shares = plan_shares(pct, window_rows, total)
    by_id = {rec['id']: rec for rec in records}
    for row, (_eid, share, plan) in zip(window_rows, shares):
        eid, score, steps, tools, kids, replies, events = row
        rec = by_id.setdefault(eid, {
            'id': eid, 'label': _bot_label(eid, nm, agents), 'cost': 0,
            'estimated': False, 'points': score, 'trend': _trend_html(None),
            'status': 'ok', 'unattributed': False})
        rec['points'] = score
        rec['plan_pct'] = plan if reading else None
        detail = [f'{nfig(replies)} replies']
        if steps:
            detail.append(f'{nfig(steps)} steps')
        if tools:
            detail.append(f'{nfig(tools)} tools')
        rec['detail'] = ' · '.join(detail)
    active = {row[0] for row in window_rows}
    for rec in by_id.values():
        if rec['id'] not in active:
            rec['points'] = 0
            rec['plan_pct'] = 0 if reading else None
    ranked = sorted(by_id.values(), key=lambda rec: (-rec['points'], -rec['cost'], rec['label'].casefold()))
    parts = ['<section class="hero">']
    if reading:
        stamp = _stamp(reading[2]) if reading[2] else reading[0]
        parts.append(f'<h1>{fig(f"{pct:g}%")} of plan used</h1>'
                     f'<p class="muted">Usage reading: {E(stamp)}</p>')
        parts.append('<div class="stack" role="img" aria-label="Estimated plan usage by bot">')
        slices = shares[:VISIBLE]
        if len(shares) > VISIBLE:
            slices.append(('others', 0, sum(row[2] for row in shares[VISIBLE:])))
        if not total and pct:
            slices.append(('unallocated', 0, pct))
        for eid, share, plan in slices:
            color = (identities.get(eid) or ('', COLORS['gray']))[1]
            label = _bot_label(eid, nm, agents) if eid in active else eid
            parts.append(f'<span style="width:{plan:.3f}%;background:{color}" title="{E(label)}: {plan:.1f}%"></span>')
        free = max(0, 100 - pct)
        parts.append(f'<span class="free" style="width:{free:.3f}%">{fig(f"free {free:g}%")}</span></div>')
    else:
        parts.append('<h1>Plan usage</h1><p class="muted">No Usage reading yet.</p>')
    parts.append(bots_section(fresh, ranked, identities, faces))
    parts.append(f'<p class="muted">Estimated bot plan usage = latest Usage % × activity share (score-v2, including subagents) from {start.isoformat()} to {end.isoformat()}.</p></section>')
    return ''.join(parts)


def build_html(db, start=None, end=None, agents=None, now=None):
    if agents is None:
        agents = paths.agents_dir()
    moment = _moment(now)
    today = moment.date()
    end = end or today
    start = start or end - timedelta(days=6)
    if start > end:
        raise ValueError('start is after end')
    fresh = report.dashboard_trends(db, today)
    faces = Avatars(agents)
    nm = report.names(db)
    cli_rows = load_cli_rows(db)
    kinds = dict(report.q(db, 'SELECT entity_id, kind FROM entities'))
    window_rows = report.window_top(db, start, end, 1000000)
    shown = set(nm) | {row[0] for row in window_rows}
    for row in cli_rows:
        if row['bot_id'] and row['bot_id'] != 'unattributed':
            shown.add(row['bot_id'])
    for row in fresh['bots']:
        shown.add(row['entity_id'])
    for eid in shown:
        # Non-uuid bot ids (a raw CLI label) stay as written. _name is for profiles.
        if re.fullmatch(r'[0-9a-fA-F-]{36}', eid or ''):
            nm[eid] = _name(eid, nm, agents)
    identities = {}
    for eid in shown:
        kind = kinds.get(eid) or ('bot' if re.fullmatch(r'[0-9a-fA-F-]{36}', eid or '') else 'unknown')
        identities[eid] = faces.icon(eid, kind)
    try:
        cfg = budgets.load_budgets(budgets.DEFAULT_BUDGETS_PATH)
    except (OSError, ValueError):
        cfg = None
    items = collect_alerts(db, today, moment, fresh, cli_rows, cfg, nm, agents)
    records = _bot_records(today, fresh, cli_rows, cfg, nm, agents)
    parts = [
        hero_section(db, fresh, records, window_rows, identities, faces, nm, agents, start, end),
        spend_section(fresh, cfg),
        '<h2>Last 30 days</h2><p class="muted">Height is activity points, stacked by bot.</p>'
        + chart(db, end - timedelta(days=29), end, nm, identities, faces),
        tasks_section(cli_rows, today),
        routines_section(db, nm, agents),
        legacy_section(db, nm),
        _footer(db),
    ]
    style = CSS + RESPONSIVE_CSS + faces.css()
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Grok Bot activity</title><style>' + style + '</style></head><body>'
            + _status_html(fresh['freshness'], moment, items, fresh)
            + '<main>' + ''.join(parts) + '</main></body></html>')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default=None, help='quota sqlite ($QUOTA_DB, else under the data root or state dir)')
    ap.add_argument('--out', default=None, help='dashboard HTML ($QUOTA_OUT, else beside the database)')
    ap.add_argument('--start', type=date.fromisoformat)
    ap.add_argument('--end', type=date.fromisoformat)
    ap.add_argument('--anonymize', action='store_true',
                    help='rewrite names, ids, paths, emails, and hosts before rendering')
    args = ap.parse_args(argv)
    if args.start and args.end and args.start > args.end:
        ap.error('--start must be on or before --end')
    db_path = args.db or paths.default_db()
    out = args.out or paths.default_out(db_path)
    profile_tmp = None
    try:
        with sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.execute('BEGIN')
            if args.anonymize:
                anon = anonymize.Anonymizer.from_db(db)
                view = anon.writable_copy(db)
                profile_tmp = tempfile.TemporaryDirectory()
                agents = anon.write_profiles(view, profile_tmp.name)
                data = build_html(view, args.start, args.end, agents=agents)
                hits = leakguard.find_hits(db, data)
            else:
                data = build_html(db, args.start, args.end)
                hits = []
    except (OSError, sqlite3.Error) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    finally:
        if profile_tmp is not None:
            profile_tmp.cleanup()
    if hits:
        print('leak guard failed:', file=sys.stderr)
        for hit in hits:
            print(hit, file=sys.stderr)
        return 1
    try:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + '.tmp')
        tmp.write_text(data, encoding='utf-8')
        os.replace(tmp, target)
    except OSError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    print(f'Dashboard: {target} ({len(data.encode("utf-8"))} bytes)')
    return 0


RESPONSIVE_CSS = """
main,.status{overflow-wrap:anywhere;min-width:0}
svg,img{max-width:100%;height:auto}
.tablewrap,.wide{overflow-x:auto;max-width:100%;-webkit-overflow-scrolling:touch}
.identity,.tool>*{min-width:0}
.hero{margin-bottom:24px}.hero h1 .fig{font-size:34px}
.hero h2{margin-top:18px}
.stack{display:flex;height:20px;overflow:hidden;background:#eaeaea;margin:12px 0}
.stack>span{display:block;flex:none;min-width:0;height:100%}
.stack .free{text-align:center;font-size:11px;line-height:20px;color:#666;white-space:nowrap}
.activity-detail{display:block;font-size:11px;margin-left:38px}
@media(max-width:700px){
.tool-trend,.tool-of{white-space:normal}
table.bots:has(.plan) td:nth-child(2)::before{content:"% of plan"}
table.bots:has(.plan) td:nth-child(3)::before{content:"CLI cost (USD)"}
table.bots:has(.plan) td:nth-child(4)::before{content:"activity points"}
table.bots:has(.plan) td:nth-child(5)::before{content:"trend"}
table.bots:has(.plan) td:nth-child(6)::before{content:"budget"}
}
"""

if __name__ == '__main__':
    sys.exit(main())

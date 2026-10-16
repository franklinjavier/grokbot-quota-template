"""Deterministic stand-ins for identifiers read from a quota database.

``Anonymizer.from_db`` learns the mapping from one read-only connection.
``writable_copy`` backups that connection into memory and rewrites text
before the dashboard renders it. The same database always yields the same
labels for the life of the process: bots are ``bot-alpha``, ``bot-bravo``,
and so on; ids become stable fake ids; paths sit under ``/home/user``;
mail becomes one example address; tailnet hosts become example hosts;
a short list of personal names becomes ``user``.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from quota import leakguard

_PHONETIC = (
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
    "xray", "yankee", "zulu",
)
_UUID_RE = re.compile(
    r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ABS_RE = re.compile(r"(?<![A-Za-z0-9])/(?:[\w.@+-]+)(?:/[\w.@+-]+)+")
_STRUCTURAL = frozenset({
    "claude", "codex", "grok", "unattributed", "unknown", "bot", "child",
    "ok", "error", "failed", "failure", "cancelled", "canceled", "null",
    "none", "true", "false", "exact", "window", "interpolated", "direct",
    "inclusive", "launch_session", "launch_cwd", "plugin", "schedule",
    "cron", "manual", "store", "audit", "transcript", "automation",
})
_COLORS = ("green", "blue", "violet", "cyan", "brown", "yellow", "red")
_SHAPES = ("blob", "squircle", "wedge", "hex", "pebble", "gem", "shield", "house")
_EXAMPLE_MAIL = "user@" + "example.com"
_EVENTS_TEXT = (
    "entity_id", "root_entity_id", "peer_id", "request_id", "turn_id",
    "run_id", "call_id", "source_id", "attr_run_id", "attr_routine_id",
)
_SMALL_TABLES = (
    ("usage_readings", ("note",)),
    ("entities", ("entity_id", "display_name", "slug")),
    ("sources", ("source_id", "entity_id")),
    ("cursors", ("source_id",)),
    ("cli_runs", ("source_id", "run_key", "task", "bot_id", "session_id", "cwd", "raw_json")),
    ("cli_launches", ("launch_key", "source_id", "bot_id", "task", "cwd", "session_id")),
    ("routines", ("owner_id", "routine_id", "name")),
    ("runs", ("owner_id", "routine_id", "run_id", "request_id")),
    ("links", ("from_id", "to_id")),
    ("detail_spans", ("entity_id",)),
    ("meta", ("key", "value")),
    ("collector_runs", ("error_summary", "tracker_request_id", "tracker_run_id")),
)


def series(prefix: str, index: int) -> str:
    """``0 -> prefix-alpha``, ``26 -> prefix-alpha-2``."""
    word = _PHONETIC[index % len(_PHONETIC)]
    cycle = index // len(_PHONETIC)
    if cycle == 0:
        return f"{prefix}-{word}"
    return f"{prefix}-{word}-{cycle + 1}"


def fake_uuid(index: int) -> str:
    """Stable id that does not use the fixture prefix."""
    return f"11111111-1111-4111-8111-{index:012x}"


class Anonymizer:
    def __init__(self):
        self.uuid_of: dict[str, str] = {}
        self.bot_of: dict[str, str] = {}
        self.bot_by_id: dict[str, str] = {}
        self.task_of: dict[str, str] = {}
        self.routine_of: dict[str, str] = {}
        self.path_of: dict[str, str] = {}
        self.host_of: dict[str, str] = {}
        self.id_of: dict[str, str] = {}
        self._spans: list[tuple[re.Pattern[str], str]] = []
        self._person_re: re.Pattern[str] | None = None
        self._host_re: re.Pattern[str] | None = None
        self._grok_re: re.Pattern[str] | None = None
        self._prefix_re: list[tuple[re.Pattern[str], str]] = []
        self._cache: dict[str, str] = {}

    @classmethod
    def from_db(cls, db) -> "Anonymizer":
        anon = cls()
        texts: list[str] = []
        entities = _rows(db, "SELECT entity_id, kind, display_name, slug FROM entities")
        tasks = _distinct(db, "cli_runs", "task") + _distinct(db, "cli_launches", "task")
        routines = _rows(db, "SELECT owner_id, routine_id, name FROM routines")
        cwds = _distinct(db, "cli_runs", "cwd") + _distinct(db, "cli_launches", "cwd")
        for table, column in (
            ("cli_runs", "session_id"), ("cli_runs", "source_id"), ("cli_runs", "run_key"),
            ("cli_runs", "bot_id"), ("cli_runs", "raw_json"),
            ("cli_launches", "session_id"), ("cli_launches", "bot_id"),
            ("runs", "run_id"), ("runs", "request_id"),
            ("sources", "source_id"), ("cursors", "source_id"),
            ("meta", "key"), ("meta", "value"),
            ("events", "source_id"), ("events", "request_id"), ("events", "turn_id"),
            ("events", "run_id"), ("events", "call_id"), ("events", "peer_id"),
        ):
            texts.extend(_distinct(db, table, column))
        for eid, _kind, name, slug in entities:
            texts.extend(item for item in (eid, name, slug) if item)
        for owner, rid, name in routines:
            texts.extend(item for item in (owner, rid, name) if item)
        texts.extend(tasks)
        texts.extend(cwds)

        uuids: set[str] = set()
        for text in texts:
            if not text:
                continue
            for match in _UUID_RE.findall(str(text)):
                uuids.add(match.casefold())
        for index, key in enumerate(sorted(uuids), start=1):
            anon.uuid_of[key] = fake_uuid(index)

        bots = []
        for eid, kind, name, slug in entities:
            if kind != "bot" or not eid:
                continue
            bots.append((str(name or ""), str(eid), str(slug or "")))
        bots.sort(key=lambda item: (item[0].casefold(), item[1].casefold()))
        for index, (name, eid, slug) in enumerate(bots):
            label = series("bot", index)
            fake_id = anon.uuid_of.get(eid.casefold())
            anon.bot_by_id[eid.casefold()] = label
            if fake_id:
                anon.bot_by_id[fake_id.casefold()] = label
            if name:
                anon.bot_of.setdefault(name.casefold(), label)
            if slug:
                anon.bot_of.setdefault(slug.casefold(), label)
        # CLI bot ids that are names rather than agent ids.
        extra_bots = []
        for table in ("cli_runs", "cli_launches"):
            for value in _distinct(db, table, "bot_id"):
                text = str(value)
                if text in ("unattributed", "unknown") or _UUID_RE.fullmatch(text):
                    continue
                if leakguard.is_safe_name(text) or text.casefold() in anon.bot_of:
                    continue
                extra_bots.append(text)
        for offset, text in enumerate(sorted(set(extra_bots), key=str.casefold)):
            anon.bot_of[text.casefold()] = series("bot", len(bots) + offset)

        task_keys = sorted({str(item).casefold() for item in tasks if item and str(item).strip()},
                           key=str.casefold)
        for index, key in enumerate(task_keys):
            anon.task_of[key] = series("task", index)

        routine_rows = []
        for owner, rid, name in routines:
            routine_rows.append((str(owner or ""), str(rid or ""), str(name or "")))
        routine_rows.sort(key=lambda item: (item[2].casefold(), item[1].casefold(), item[0].casefold()))
        for index, (_owner, rid, name) in enumerate(routine_rows):
            label = series("routine", index)
            if rid:
                anon.routine_of[rid.casefold()] = label
            if name:
                anon.routine_of.setdefault(name.casefold(), label)

        paths: set[str] = set()
        for text in list(cwds) + texts:
            if not text:
                continue
            raw = str(text)
            if raw.startswith("/") or raw.startswith("~"):
                paths.add(raw)
            for match in _ABS_RE.findall(raw):
                paths.add(match)
            decoded = raw.replace("%2F", "/").replace("%2f", "/")
            if decoded != raw:
                for match in _ABS_RE.findall(decoded):
                    paths.add(match)
        for index, path in enumerate(sorted(paths, key=str.casefold), start=1):
            anon.path_of[path] = f"/home/user/p-{index:02d}"

        suffix = leakguard.host_suffix()
        host_re = re.compile(
            r"(?i)(?<![A-Za-z0-9-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*" + re.escape(suffix))
        token = leakguard.grok_bot_token()
        grok_re = re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(token) + r"[A-Za-z0-9.-]*")
        hosts: set[str] = set()
        for text in texts:
            if not text:
                continue
            for match in host_re.findall(str(text)):
                hosts.add(match.casefold())
            for match in grok_re.findall(str(text)):
                hosts.add(match.casefold())
        for index, host in enumerate(sorted(hosts), start=1):
            anon.host_of[host] = f"host-{index:02d}.example.com"

        other: set[str] = set()
        for table, column in (
            ("cli_runs", "session_id"), ("cli_launches", "session_id"),
            ("runs", "run_id"), ("runs", "request_id"),
            ("events", "request_id"), ("events", "turn_id"), ("events", "run_id"),
            ("collector_runs", "tracker_request_id"), ("collector_runs", "tracker_run_id"),
        ):
            for value in _distinct(db, table, column):
                text = str(value).strip()
                if not text or text.casefold() in anon.uuid_of or _UUID_RE.fullmatch(text):
                    continue
                if "\n" in text or len(text) > 200:
                    continue
                other.add(text)
        for index, text in enumerate(sorted(other, key=str.casefold), start=1):
            anon.id_of[text] = f"id-{index:04x}"

        anon._compile()
        return anon

    def _compile(self):
        people = leakguard.person_names()
        if people:
            self._person_re = re.compile(
                r"(?i)(?<![A-Za-z0-9])(?:" + "|".join(re.escape(name) for name in people) + r")(?![A-Za-z0-9])")
        suffix = leakguard.host_suffix()
        self._host_re = re.compile(
            r"(?i)(?<![A-Za-z0-9-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*" + re.escape(suffix))
        token = leakguard.grok_bot_token()
        self._grok_re = re.compile(
            r"(?i)(?<![A-Za-z0-9])" + re.escape(token) + r"[A-Za-z0-9.-]*")
        prefixes = []
        for word in leakguard.fixed_substrings():
            if not word.startswith("/"):
                continue
            generic = "/home/user"
            if word.endswith("space") or "work" in word:
                generic = "/home/user/tree"
            elif "cursor" in word:
                generic = "/home/user/cache"
            prefixes.append((re.compile(re.escape(word), re.I), generic))
            encoded = word.replace("/", "%2F")
            prefixes.append((re.compile(re.escape(encoded), re.I), generic.replace("/", "%2F")))
            prefixes.append((re.compile(re.escape(word.replace("/", "%2f")), re.I),
                             generic.replace("/", "%2f")))
        self._prefix_re = prefixes
        tail = next(word for word in leakguard.fixed_substrings() if word.startswith("tail"))
        self._tail = tail
        spans: list[tuple[str, str]] = []
        for real, fake in self.path_of.items():
            spans.append((real, fake))
        for key, fake in self.bot_of.items():
            if leakguard.is_safe_name(key) or len(key) < 2:
                continue
            spans.append((key, fake))
        for key, fake in self.task_of.items():
            if len(key) < 4:
                continue
            spans.append((key, fake))
        for key, fake in self.routine_of.items():
            if len(key) < 4:
                continue
            spans.append((key, fake))
        spans.sort(key=lambda item: len(item[0]), reverse=True)
        self._spans = [
            (re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(real) + r"(?![A-Za-z0-9])"), fake)
            for real, fake in spans
            if not real.startswith("/")
        ]
        # Paths are not token-bounded the same way; keep them as literal substrings.
        self._path_spans = [
            (real, fake) for real, fake in sorted(self.path_of.items(), key=lambda item: len(item[0]), reverse=True)
        ]

    def rewrite(self, value, role=None):
        """Return the stand-in for one database string. ``None`` stays ``None``."""
        if value is None:
            return None
        text = str(value)
        key = text if role is None else role + "\0" + text
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        rewritten = self._rewrite(text, role)
        if len(self._cache) < 200000:
            self._cache[key] = rewritten
        return rewritten

    def _rewrite(self, text: str, role=None) -> str:
        folded = text.casefold()
        # Label columns keep their own map so a bot called the same thing as a
        # task does not swap the two stand-ins.
        if role in ("display_name", "slug") and folded in self.bot_of:
            return self.bot_of[folded]
        if role == "task" and folded in self.task_of:
            return self.task_of[folded]
        if role in ("name", "routine_id") and folded in self.routine_of:
            return self.routine_of[folded]
        if role in ("cwd", "path") and text in self.path_of:
            return self.path_of[text]
        if text in _STRUCTURAL:
            return text
        if folded in self.uuid_of and _UUID_RE.fullmatch(text):
            return self.uuid_of[folded]
        if text in self.id_of:
            return self.id_of[text]
        if folded in self.routine_of:
            return self.routine_of[folded]
        if folded in self.task_of and not leakguard.is_safe_name(folded):
            return self.task_of[folded]
        if folded in self.bot_of and not leakguard.is_safe_name(folded):
            return self.bot_of[folded]
        if text in self.path_of:
            return self.path_of[text]
        return self._spans_apply(text)

    def _spans_apply(self, text: str) -> str:
        for real, fake in self._path_spans:
            if real in text:
                text = text.replace(real, fake)
        if self.uuid_of and _UUID_RE.search(text):
            text = _UUID_RE.sub(lambda match: self.uuid_of.get(match.group(0).casefold(), self._fallback_uuid(match.group(0))), text)
        text = _EMAIL_RE.sub(_EXAMPLE_MAIL, text)
        if self._host_re is not None:
            text = self._host_re.sub(lambda match: self.host_of.get(match.group(0).casefold(), "host-00.example.com"), text)
        if self._grok_re is not None:
            text = self._grok_re.sub(lambda match: self.host_of.get(match.group(0).casefold(), "host-00.example.com"), text)
        for pattern, repl in self._prefix_re:
            text = pattern.sub(repl, text)
        if self._tail and self._tail.casefold() in text.casefold():
            text = re.sub(re.escape(self._tail), "tail000000", text, flags=re.I)
        for pattern, repl in self._spans:
            text = pattern.sub(repl, text)
        if self._person_re is not None:
            text = self._person_re.sub("user", text)
        if _ABS_RE.search(text):
            def _abs_sub(match):
                found = match.group(0)
                if found.startswith("/home/user"):
                    return found
                return self.path_of.get(found, "/home/user/other")
            text = _ABS_RE.sub(_abs_sub, text)
        return text

    def _fallback_uuid(self, value: str) -> str:
        key = value.casefold()
        if key not in self.uuid_of:
            self.uuid_of[key] = fake_uuid(len(self.uuid_of) + 1)
        return self.uuid_of[key]

    def label_for_bot(self, entity_id, display_name=None) -> str:
        if entity_id and str(entity_id).casefold() in self.bot_of:
            return self.bot_of[str(entity_id).casefold()]
        if display_name and str(display_name).casefold() in self.bot_of:
            return self.bot_of[str(display_name).casefold()]
        return self.rewrite(display_name or entity_id)

    def writable_copy(self, db) -> sqlite3.Connection:
        """In-memory copy of ``db`` with identifiers rewritten. ``db`` is not written."""
        mem = sqlite3.connect(":memory:")
        db.backup(mem)
        self.apply(mem)
        return mem

    def apply(self, db) -> None:
        """Rewrite text columns of a writable connection in place."""
        db.execute("PRAGMA synchronous=OFF")
        self._apply_events(db)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, columns in _SMALL_TABLES:
            if table not in tables:
                continue
            present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            usable = [column for column in columns if column in present]
            self._apply_table(db, table, usable)
        # Bots with no stored name still need a stand-in label.
        if "entities" in tables:
            rows = db.execute(
                "SELECT entity_id, display_name FROM entities WHERE kind='bot'").fetchall()
            for eid, name in rows:
                label = self.bot_by_id.get(str(eid).casefold())
                if label and name != label:
                    db.execute(
                        "UPDATE entities SET display_name=? WHERE entity_id=?", (label, eid))

    def _apply_events(self, db):
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "events" not in tables:
            return
        present = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        columns = [column for column in _EVENTS_TEXT if column in present]
        if not columns:
            return
        select = "rowid, " + ", ".join(columns)
        rows = db.execute(f"SELECT {select} FROM events").fetchall()
        updates = []
        for row in rows:
            rowid = row[0]
            values = list(row[1:])
            changed = False
            new_values = []
            for column, value in zip(columns, values):
                if value is None:
                    new_values.append(None)
                    continue
                rewritten = self.rewrite(value, column)
                if rewritten != value:
                    changed = True
                new_values.append(rewritten)
            if changed:
                updates.append((*new_values, rowid))
        if not updates:
            return
        assignments = ", ".join(f"{column}=?" for column in columns)
        db.executemany(f"UPDATE events SET {assignments} WHERE rowid=?", updates)

    def _apply_table(self, db, table: str, columns: list[str]):
        if not columns:
            return
        select = "rowid, " + ", ".join(columns)
        rows = db.execute(f"SELECT {select} FROM {table}").fetchall()
        updates = []
        for row in rows:
            values = []
            changed = False
            for column, value in zip(columns, row[1:]):
                if not isinstance(value, str):
                    values.append(value)
                    continue
                rewritten = self.rewrite(value, column)
                if rewritten != value:
                    changed = True
                values.append(rewritten)
            if changed:
                updates.append((*values, row[0]))
        if not updates:
            return
        assignments = ", ".join(f"{column}=?" for column in columns)
        db.executemany(f"UPDATE {table} SET {assignments} WHERE rowid=?", updates)

    def write_profiles(self, db, root) -> str:
        """Fake profile.json files keyed by the already anonymized agent id.

        Colors and shapes only. The name is the stand-in label. No photos.
        """
        base = Path(root)
        base.mkdir(parents=True, exist_ok=True)
        rows = db.execute(
            "SELECT entity_id, display_name FROM entities WHERE kind='bot' ORDER BY display_name, entity_id"
        ).fetchall()
        for index, (eid, name) in enumerate(rows):
            if not eid:
                continue
            folder = base / str(eid)
            folder.mkdir(parents=True, exist_ok=True)
            payload = {
                "name": name or "bot",
                "avatarColor": _COLORS[index % len(_COLORS)],
                "avatarShape": _SHAPES[index % len(_SHAPES)],
            }
            (folder / "profile.json").write_text(json.dumps(payload), encoding="utf-8")
        return str(base)


def _rows(db, sql: str):
    try:
        return db.execute(sql).fetchall()
    except sqlite3.Error:
        return []


def _distinct(db, table: str, column: str) -> list:
    try:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return []
        cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            return []
        return [row[0] for row in db.execute(
            f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL")]
    except sqlite3.Error:
        return []

#!/usr/bin/env python3
"""Fail when a file still contains real identifiers from a quota database.

Usage:
  python3 -m quota.leakguard --db DB FILE...

The database is opened read-only. Needles are bot names, agent ids, working
directories and other absolute paths, every UUID stored in the database, and
a fixed denylist (personal names, home and workspace prefixes, tailnet hosts,
mail domains). A hit prints ``PATH: term`` and the process exits 1.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

# ASCII hex so this file does not contain the denylist text.
_HEX = {
    "n_long": "6672616e6b6c696e",
    "n_short": "6672616e6b",
    "n_other": "6a6176696572",
    "home_box": "2f686f6d652f626f78",
    "workspace": "2f776f726b7370616365",
    "cursor_stores": "2f637572736f722f73746f726573",
    "gmail": "40676d61696c",
    "tail": "7461696c383935383737",
    "ts_net": "2e74732e6e6574",
    "grok_bot": "67726f6b2d626f74",
}

# Single tokens that are this tracker's own vocabulary. They are not
# distinctive enough to search for as a bot name.
_SAFE_NAMES = frozenset({
    "grok", "quota", "claude", "codex", "bot", "bots", "user", "test", "tests",
    "unattributed", "unknown", "child", "children", "default", "null", "none",
    "true", "false", "ok", "error",
})

_UUID_RE = re.compile(
    r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID_COLUMNS = (
    ("entities", "entity_id"),
    ("entities", "display_name"),
    ("entities", "slug"),
    ("sources", "entity_id"),
    ("sources", "source_id"),
    ("cli_runs", "bot_id"),
    ("cli_runs", "session_id"),
    ("cli_runs", "task"),
    ("cli_runs", "cwd"),
    ("cli_runs", "source_id"),
    ("cli_runs", "run_key"),
    ("cli_launches", "bot_id"),
    ("cli_launches", "session_id"),
    ("cli_launches", "task"),
    ("cli_launches", "cwd"),
    ("routines", "owner_id"),
    ("routines", "routine_id"),
    ("routines", "name"),
    ("runs", "owner_id"),
    ("runs", "routine_id"),
    ("runs", "run_id"),
    ("runs", "request_id"),
    ("events", "entity_id"),
    ("events", "peer_id"),
    ("events", "root_entity_id"),
    ("events", "request_id"),
    ("events", "turn_id"),
    ("events", "run_id"),
    ("events", "source_id"),
    ("links", "from_id"),
    ("links", "to_id"),
    ("meta", "key"),
    ("meta", "value"),
    ("cursors", "source_id"),
    ("collector_runs", "error_summary"),
    ("collector_runs", "tracker_request_id"),
    ("collector_runs", "tracker_run_id"),
)


def _word(key: str) -> str:
    return bytes.fromhex(_HEX[key]).decode("ascii")


def fixed_substrings() -> list[str]:
    """Decoded denylist fragments, longest first."""
    keys = (
        "n_long", "n_other", "n_short", "home_box", "workspace", "cursor_stores",
        "gmail", "tail", "ts_net", "grok_bot",
    )
    words = [_word(key) for key in keys]
    words.sort(key=len, reverse=True)
    return words


def is_safe_name(value) -> bool:
    """True for this tracker's own vocabulary, which is not a distinctive bot name."""
    return str(value or "").strip().casefold() in _SAFE_NAMES


def person_names() -> list[str]:
    """Personal-name denylist entries, longest first."""
    names = [_word("n_long"), _word("n_short"), _word("n_other")]
    names.sort(key=len, reverse=True)
    return names


def host_suffix() -> str:
    return _word("ts_net")


def grok_bot_token() -> str:
    return _word("grok_bot")


def _tables(db) -> set[str]:
    return {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _has(tables: set[str], table: str, column: str, db) -> bool:
    if table not in tables:
        return False
    cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    return column in cols


class Needles:
    """Identifiers to search for. ``names`` use token boundaries."""

    def __init__(self):
        self.names: list[str] = []
        self.paths: list[str] = []
        self.uuids: set[str] = set()
        self._name_seen: set[str] = set()
        self._path_seen: set[str] = set()

    def add_name(self, value):
        if value is None:
            return
        text = str(value).strip()
        if len(text) < 2:
            return
        key = text.casefold()
        if key in _SAFE_NAMES or key in self._name_seen:
            return
        if _UUID_RE.fullmatch(text):
            self.add_uuid(text)
            return
        self._name_seen.add(key)
        self.names.append(text)

    def add_path(self, value):
        if value is None:
            return
        text = str(value).strip()
        if len(text) < 2:
            return
        key = text.casefold()
        if key in self._path_seen:
            return
        self._path_seen.add(key)
        self.paths.append(text)

    def add_uuid(self, value):
        if not value:
            return
        self.uuids.add(str(value).casefold())

    def absorb_text(self, value):
        if not value:
            return
        text = str(value)
        for match in _UUID_RE.findall(text):
            self.add_uuid(match)
        if text.startswith("/") or text.startswith("~") or "://" in text:
            self.add_path(text)
        lowered = text.casefold()
        for word in fixed_substrings():
            if word.casefold() in lowered and (word.startswith("/") or word.startswith("%")):
                self.add_path(text)
                break


def collect_needles(db) -> Needles:
    """Bot names, agent ids, cwd values, paths, and UUIDs from ``db``."""
    needles = Needles()
    tables = _tables(db)
    if _has(tables, "entities", "display_name", db):
        for eid, name, slug in db.execute(
                "SELECT entity_id, display_name, slug FROM entities"):
            needles.add_name(name)
            needles.add_name(slug)
            if eid:
                needles.add_uuid(eid) if _UUID_RE.fullmatch(str(eid)) else needles.add_name(eid)
                needles.absorb_text(eid)
    for table, column in _ID_COLUMNS:
        if not _has(tables, table, column, db):
            continue
        # Distinct keeps the events scan on an index-friendly column.
        try:
            rows = db.execute(
                f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL")
        except sqlite3.Error:
            continue
        for (value,) in rows:
            needles.absorb_text(value)
            if column in ("display_name", "slug"):
                needles.add_name(value)
            elif column == "cwd" or (isinstance(value, str) and value.startswith("/")):
                needles.add_path(value)
            elif column in ("bot_id",) and value and not _UUID_RE.fullmatch(str(value)):
                if str(value) not in ("unattributed", "unknown"):
                    needles.add_name(value)
    # URL-encoded forms of the fixed path prefixes.
    for word in fixed_substrings():
        if word.startswith("/"):
            needles.add_path(word.replace("/", "%2F"))
            needles.add_path(word.replace("/", "%2f"))
    return needles


def _bounded(hay: str, needle: str) -> bool:
    return re.search(
        r"(?<![a-z0-9])" + re.escape(needle.casefold()) + r"(?![a-z0-9])",
        hay) is not None


def hits_in_text(text: str, needles: Needles) -> list[str]:
    """Return the needles and denylist patterns present in ``text``."""
    blob = text or ""
    hay = blob.casefold()
    found: list[str] = []
    seen: set[str] = set()

    def add(label: str):
        key = label.casefold()
        if not key or key in seen:
            return
        seen.add(key)
        found.append(label)

    for name in needles.names:
        if _bounded(hay, name):
            add(name)
    for path in needles.paths:
        if path.casefold() in hay:
            add(path)
    if needles.uuids:
        for match in _UUID_RE.findall(blob):
            if match.casefold() in needles.uuids:
                add(match)
    for word in fixed_substrings():
        if word.casefold() in hay:
            add(word)
    suffix = host_suffix()
    host_re = re.compile(
        r"(?i)(?<![A-Za-z0-9-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*" + re.escape(suffix))
    for match in host_re.findall(blob):
        add(match)
    token = grok_bot_token()
    grok_re = re.compile(
        r"(?i)(?<![a-z0-9])" + re.escape(token) + r"[a-z0-9.-]*")
    for match in grok_re.findall(blob):
        add(match)
    for match in _EMAIL_RE.findall(blob):
        add(match)
    found.sort(key=str.casefold)
    return found


def find_hits(db, text: str) -> list[str]:
    """Collect needles from ``db`` and return those present in ``text``."""
    return hits_in_text(text, collect_needles(db))


def open_ro(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="quota sqlite (opened read-only)")
    parser.add_argument("files", nargs="+", help="files to scan")
    args = parser.parse_args(argv)
    try:
        db = open_ro(args.db)
    except (OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        db.execute("BEGIN")
        needles = collect_needles(db)
    except sqlite3.Error as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()
    failed = False
    for raw in args.files:
        path = Path(raw)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        for hit in hits_in_text(text, needles):
            failed = True
            print(f"{path}: {hit}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Example attribution plugin. Not used unless you enable it.

Reads a sqlite file of projects and tasks. The path is ``$QUOTA_TASKS_DB``
or the ``tasks_db`` string in the quota config. The file is opened read-only.
A missing path returns no bot.

Schema::

    CREATE TABLE projects (id TEXT, name TEXT, bot_id TEXT);
    CREATE TABLE tasks (id TEXT, project_id TEXT);

A task id wins. Otherwise the longest project id or name contained in the
cwd or task string wins (case-insensitive, at least 2 characters).
"""

from __future__ import annotations

import os
import sqlite3

_CACHE: dict = {}


def tasks_path(config) -> str | None:
    env = os.environ.get("QUOTA_TASKS_DB")
    if env:
        return os.path.expanduser(env)
    if isinstance(config, dict):
        raw = config.get("tasks_db")
        if isinstance(raw, str) and raw.strip():
            return os.path.expanduser(raw)
    return None


class TaskIndex:
    """Read-only map of a tasks sqlite onto a bot id."""

    def __init__(self, path: str):
        self.by_task: dict[str, str | None] = {}
        self.by_task_cf: dict[str, str | None] = {}
        self.needles: list[tuple[str, str]] = []
        self._load(os.path.abspath(path))

    def _load(self, path: str) -> None:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            con.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "projects" not in tables:
                return
            columns = {row[1] for row in con.execute("PRAGMA table_info(projects)")}
            if "id" not in columns or "bot_id" not in columns:
                return
            name_expr = "name" if "name" in columns else "NULL"
            bots: dict[str, str | None] = {}
            needles = []
            for pid, bot, name in con.execute(f"SELECT id, bot_id, {name_expr} FROM projects"):
                if not isinstance(pid, str) or not pid:
                    continue
                bot_ok = bot if isinstance(bot, str) and bot.strip() else None
                bots[pid] = bot_ok
                if not bot_ok:
                    continue
                needles.append((pid.casefold(), bot_ok))
                if isinstance(name, str) and name.strip() and name.casefold() != pid.casefold():
                    needles.append((name.casefold(), bot_ok))
            needles.sort(key=lambda item: (-len(item[0]), item[0]))
            seen = set()
            for needle, bot in needles:
                if len(needle) < 2 or needle in seen:
                    continue
                seen.add(needle)
                self.needles.append((needle, bot))
            if "tasks" not in tables:
                return
            tcols = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
            if "id" not in tcols or "project_id" not in tcols:
                return
            for tid, pid in con.execute("SELECT id, project_id FROM tasks"):
                if not isinstance(tid, str) or not tid:
                    continue
                bot = bots.get(pid)
                self.by_task[tid] = bot
                self.by_task_cf.setdefault(tid.casefold(), bot)
        finally:
            con.close()

    def bot_for(self, task=None, cwd=None) -> str:
        if isinstance(task, str) and task:
            if task in self.by_task:
                return self.by_task[task] or "unattributed"
            folded = task.casefold()
            if folded in self.by_task_cf:
                return self.by_task_cf[folded] or "unattributed"
        parts = []
        if isinstance(cwd, str) and cwd:
            parts.append(cwd)
        if isinstance(task, str) and task:
            parts.append(task)
        blob = " ".join(parts).casefold()
        if blob:
            for needle, bot in self.needles:
                if needle in blob:
                    return bot
        return "unattributed"


def cached_index(path: str) -> TaskIndex:
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    hit = _CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    index = TaskIndex(path)
    _CACHE[path] = (key, index)
    return index


def lookup(task, cwd, config=None):
    """Plugin entry point. Returns a bot id, or None."""
    path = tasks_path(config)
    if not path or not os.path.isfile(path):
        return None
    bot = cached_index(path).bot_for(task, cwd)
    if not bot or bot == "unattributed":
        return None
    return bot

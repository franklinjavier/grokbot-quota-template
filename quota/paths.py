"""Default locations. No username or machine name is hardcoded.

Data root, in order, first directory that exists:

1. ``$QUOTA_DATA_ROOT``
2. ``~/agent-data``
3. ``/home/*/agent-data`` discovered from ``Path.home()`` (the parent of the
   home directory, so the username is never written down)

The quota database is ``$QUOTA_DB``, else ``<data-root>/quota/quota.db`` when
a data root exists, else ``<state-dir>/quota.db``.

The state directory is ``$GROKBOT_QUOTA_STATE`` when that is set. Otherwise it
is ``~/.local/state/grokbot-quota`` when that path can be created, and
``~/.local/share/grokbot-quota`` when it cannot.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def expand(value: str) -> str:
    return os.path.expanduser(value)


def home() -> Path:
    return Path.home()


def _can_create(path: Path) -> bool:
    """True when ``path`` is a writable directory or could be created under one."""
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK)
    cur = path
    while not cur.exists():
        parent = cur.parent
        if parent == cur:
            return False
        cur = parent
    return cur.is_dir() and os.access(cur, os.W_OK)


def state_dir() -> Path:
    """State directory. Does not create it."""
    raw = os.environ.get("GROKBOT_QUOTA_STATE")
    if raw:
        return Path(expand(raw))
    state = home() / ".local" / "state" / "grokbot-quota"
    share = home() / ".local" / "share" / "grokbot-quota"
    if _can_create(state):
        return state
    return share


def existing_data_root() -> Path | None:
    """First existing data root, or None."""
    raw = os.environ.get("QUOTA_DATA_ROOT")
    if raw:
        path = Path(expand(raw))
        if path.is_dir():
            return path
    own = home() / "agent-data"
    if own.is_dir():
        return own
    parent = home().parent
    try:
        matches = sorted(p for p in parent.glob("*/agent-data") if p.is_dir())
    except OSError:
        matches = []
    for match in matches:
        if match != own:
            return match
    return None


def default_data_root() -> str:
    """Existing root, else ``$QUOTA_DATA_ROOT``, else ``~/agent-data``."""
    found = existing_data_root()
    if found is not None:
        return str(found)
    raw = os.environ.get("QUOTA_DATA_ROOT")
    if raw:
        return expand(raw)
    return str(home() / "agent-data")


def default_db() -> str:
    raw = os.environ.get("QUOTA_DB")
    if raw:
        return expand(raw)
    root = existing_data_root()
    if root is not None:
        return str(root / "quota" / "quota.db")
    return str(state_dir() / "quota.db")


def default_out(db_path: str | None = None) -> str:
    """Dashboard HTML. ``$QUOTA_OUT`` wins, else beside the database."""
    raw = os.environ.get("QUOTA_OUT")
    if raw:
        return expand(raw)
    base = db_path or default_db()
    return str(Path(base).resolve().parent / "dashboard.html")


def agents_dir() -> Path:
    root = existing_data_root()
    if root is None:
        root = Path(default_data_root())
    return root / "agents"


def default_codex_sessions() -> str:
    raw = os.environ.get("QUOTA_CODEX_SESSIONS")
    if raw:
        return expand(raw)
    return str(home() / ".codex" / "sessions")


def default_grok_sessions() -> str:
    raw = os.environ.get("QUOTA_GROK_SESSIONS")
    if raw:
        return expand(raw)
    return str(home() / ".grok" / "sessions")


def report_globs(root: str) -> list[str]:
    """Claude report globs.

    ``$QUOTA_REPORTS_GLOB`` may list several patterns separated by
    ``os.pathsep`` (``:`` on Linux). The default is two levels under
    ``<root>/reports``.
    """
    raw = os.environ.get("QUOTA_REPORTS_GLOB")
    if raw:
        return [expand(part) for part in raw.split(os.pathsep) if part]
    return [
        os.path.join(root, "reports", "*", "claude-*.jsonl"),
        os.path.join(root, "reports", "*", "*", "claude-*.jsonl"),
    ]


def default_launches_path() -> str:
    """Single path grokbot-run writes, before the share-directory fallback."""
    raw = os.environ.get("GROKBOT_QUOTA_STATE")
    if raw:
        return os.path.join(expand(raw), "launches.jsonl")
    return str(home() / ".local" / "state" / "grokbot-quota" / "launches.jsonl")


def default_launches_specs() -> list[tuple[str, str]]:
    """``(path, source_id)`` files to read.

    ``GROKBOT_QUOTA_STATE`` selects that one file, same as grokbot-run.
    Otherwise both the state and share logs are candidates; missing files
    are skipped by the collector.
    """
    raw = os.environ.get("GROKBOT_QUOTA_STATE")
    if raw:
        return [(os.path.join(expand(raw), "launches.jsonl"), "launches.jsonl")]
    return [
        (str(home() / ".local" / "state" / "grokbot-quota" / "launches.jsonl"), "launches.jsonl"),
        (str(home() / ".local" / "share" / "grokbot-quota" / "launches.jsonl"), "share/launches.jsonl"),
    ]

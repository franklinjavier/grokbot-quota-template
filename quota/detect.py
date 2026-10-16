"""Print which quota sources exist. Read-only, glob and stat only.

Usage:
  python3 -m quota.detect
"""

from __future__ import annotations

import glob
import os
import sys

from quota import paths


def _files(patterns: list[str]) -> list[str]:
    found = []
    seen = set()
    for pattern in patterns:
        try:
            matches = glob.glob(pattern)
        except OSError:
            continue
        for path in matches:
            if not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            found.append(path)
    return found


def _measure(file_list: list[str]) -> tuple[int, int]:
    count = 0
    size = 0
    for path in file_list:
        try:
            size += os.path.getsize(path)
        except OSError:
            continue
        count += 1
    return count, size


def _line(label: str, file_list: list[str]) -> str:
    count, size = _measure(file_list)
    unit = "file" if count == 1 else "files"
    return f"{label}: {count} {unit}, {size} bytes"


def report() -> str:
    """One block of text. Does not create directories or open databases."""
    root = paths.existing_data_root()
    lines = []
    if root is None:
        lines.append("agent-data root: ABSENT")
        root_s = None
    else:
        lines.append(f"agent-data root: PRESENT {root}")
        root_s = str(root)

    def under(*parts: str) -> list[str]:
        if root_s is None:
            return []
        return _files([os.path.join(root_s, *parts)])

    lines.append(_line("search-index.db", under("search-index.db")))
    lines.append(_line("agents/audit.jsonl", under("agents", "*", "audit.jsonl")))
    lines.append(_line("agents/store.db", under("agents", "*", "store.db")))
    lines.append(_line("agent-transcripts", under("agent-transcripts", "*.jsonl")
                        + under("agent-transcripts", "*", "*.jsonl")))
    globs = paths.report_globs(root_s) if root_s else []
    lines.append(_line("claude reports", _files(globs)))
    codex = paths.default_codex_sessions()
    lines.append(_line("codex sessions", _files([os.path.join(codex, "*", "*", "*", "rollout-*.jsonl")])))
    grok = paths.default_grok_sessions()
    lines.append(_line("grok sessions", _files([os.path.join(grok, "*", "*", "usage.json")])))
    launch_paths = [path for path, _sid in paths.default_launches_specs()]
    lines.append(_line("launches.jsonl", _files(launch_paths)))
    return "\n".join(lines)


def main(argv=None) -> int:
    del argv
    print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())

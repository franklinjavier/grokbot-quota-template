#!/usr/bin/env python3
"""Hand-entered quota readings and a small least-squares calibration.

Usage:
  python3 -m quota.usage add YYYY-MM-DD PCT [--note TEXT] [--db PATH]
  python3 -m quota.usage list [--db PATH]
  python3 -m quota.usage rm YYYY-MM-DD [--db PATH]
  python3 -m quota.usage fit [--db PATH]

`fit` regresses the daily change in usage_pct on assistant messages, shell
commands and assistant-or-user characters (in thousands) from the quota DB.
A negative change is a new quota period: that reading's own percentage is the
delta. Coefficients and R^2 are printed only when there are at least 7 deltas.
"""

from __future__ import annotations

import argparse
import os
import math
import sqlite3
import sys
from datetime import date, timedelta
from itertools import combinations

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota import paths
from quota.collect import now_ms, open_db  # noqa: E402

MIN_DELTAS = 7
FEATURES = ("assistant_messages", "shell_commands", "chars_k")
LETTERS = {"assistant_messages": "a", "shell_commands": "b", "chars_k": "c"}


def deltas(readings) -> list[tuple[str, str, float]]:
    """Consecutive recorded days → (prev_day, day, delta).

    A negative change starts a new period; the new percentage is the delta.
    """
    rows = sorted(readings, key=lambda item: item[0])
    out = []
    prev = None
    for day, pct in rows:
        if prev is None:
            prev = (day, float(pct))
            continue
        change = float(pct) - prev[1]
        if change < 0:
            change = float(pct)
        out.append((prev[0], day, change))
        prev = (day, float(pct))
    return out


def window_days(prev_day: str, day: str) -> list[str]:
    """Calendar dates after prev_day up to and including day."""
    start = date.fromisoformat(prev_day) + timedelta(days=1)
    end = date.fromisoformat(day)
    out = []
    while start <= end:
        out.append(start.isoformat())
        start += timedelta(days=1)
    return out


def _table(db, name) -> bool:
    row = db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table', 'view')", (name,)).fetchone()
    return bool(row)


def activity_by_day(db) -> tuple[dict, dict, dict]:
    """day → assistant messages, shell commands, characters (all roles)."""
    messages, shells, chars = {}, {}, {}
    if _table(db, "bot_activity_daily"):
        try:
            rows = db.execute("SELECT day, role, messages, chars FROM bot_activity_daily").fetchall()
        except sqlite3.Error:
            rows = []
        for day, role, count, size in rows:
            if not day:
                continue
            if role == "assistant":
                messages[day] = messages.get(day, 0.0) + float(count or 0)
            chars[day] = chars.get(day, 0.0) + float(size or 0)
    if _table(db, "shell_commands"):
        try:
            rows = db.execute("SELECT day, COUNT(*) FROM shell_commands GROUP BY day").fetchall()
        except sqlite3.Error:
            rows = []
        for day, count in rows:
            if day:
                shells[day] = float(count or 0)
    return messages, shells, chars


def fit_points(db) -> list[tuple[float, list[float]]]:
    """(delta, [assistant_messages, shell_commands, chars_k]) for each delta."""
    if not _table(db, "usage_readings"):
        return []
    readings = [(row[0], row[1]) for row in db.execute(
        "SELECT day, usage_pct FROM usage_readings ORDER BY day")]
    messages, shells, chars = activity_by_day(db)
    points = []
    for prev, day, change in deltas(readings):
        span = window_days(prev, day)
        msg = sum(messages.get(d, 0.0) for d in span)
        sh = sum(shells.get(d, 0.0) for d in span)
        ch = sum(chars.get(d, 0.0) for d in span) / 1000.0
        points.append((change, [msg, sh, ch]))
    return points


def _solve(matrix, vector):
    """Solve matrix @ beta = vector. None when the matrix is singular."""
    n = len(vector)
    aug = [list(map(float, row)) + [float(vector[i])] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-10:
            return None
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        div = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= div
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor == 0.0:
                continue
            for j in range(col, n + 1):
                aug[r][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]


def regress(points) -> tuple[dict | None, float | None]:
    """Least squares with no intercept. Drops features until the system is solvable.

    Subsets are tried largest first, dropping later features before earlier ones.
    """
    nfeat = len(FEATURES)
    for k in range(nfeat, 0, -1):
        for idxs in combinations(range(nfeat), k):
            cols = list(idxs)
            xtx = [[0.0] * k for _ in range(k)]
            xty = [0.0] * k
            for y, xs in points:
                picked = [xs[i] for i in cols]
                for i in range(k):
                    xty[i] += picked[i] * y
                    for j in range(k):
                        xtx[i][j] += picked[i] * picked[j]
            beta = _solve(xtx, xty)
            if beta is None:
                continue
            ys, pred = [], []
            for y, xs in points:
                hat = sum(beta[j] * xs[cols[j]] for j in range(k))
                ys.append(y)
                pred.append(hat)
            mean = sum(ys) / len(ys)
            ss_tot = sum((y - mean) ** 2 for y in ys)
            ss_res = sum((y - hat) ** 2 for y, hat in zip(ys, pred))
            if ss_tot == 0:
                r2 = 1.0 if ss_res == 0 else 0.0
            else:
                r2 = 1.0 - ss_res / ss_tot
            coef = {FEATURES[cols[j]]: beta[j] for j in range(k)}
            return coef, r2
    return None, None


def reading_count(db) -> int:
    if not _table(db, "usage_readings"):
        return 0
    try:
        return int(db.execute("SELECT COUNT(*) FROM usage_readings").fetchone()[0])
    except sqlite3.Error:
        return 0


def fit_text(db) -> str:
    """Coefficient line, or `need N more readings` when fewer than 7 deltas exist."""
    points = fit_points(db)
    short = MIN_DELTAS - len(points)
    if short > 0:
        return f"need {short} more readings"
    coef, r2 = regress(points)
    if not coef or r2 is None:
        return "could not fit"
    bits = [f"{LETTERS[name]}={coef[name]:.6f}" for name in FEATURES if name in coef]
    bits.append(f"R^2={r2:.6f}")
    return " ".join(bits)


def _parse_day(value: str) -> str:
    date.fromisoformat(value)
    return value


def _fmt_pct(value) -> str:
    return f"{float(value):g}"


def cmd_add(db, day: str, pct: float, note: str | None) -> None:
    _parse_day(day)
    if not math.isfinite(pct) or not 0 <= pct <= 100:
        raise ValueError("usage percentage must be between 0 and 100")
    db.execute(
        "INSERT INTO usage_readings (day, usage_pct, note, recorded_at_ms) VALUES (?,?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET usage_pct=excluded.usage_pct, note=excluded.note, "
        "recorded_at_ms=excluded.recorded_at_ms",
        (day, float(pct), note, now_ms()))
    db.commit()


def cmd_list(db) -> str:
    if not _table(db, "usage_readings"):
        return ""
    lines = []
    for day, pct, note, recorded in db.execute(
            "SELECT day, usage_pct, note, recorded_at_ms FROM usage_readings ORDER BY day"):
        lines.append(f"{day}\t{_fmt_pct(pct)}\t{note or ''}\t{recorded if recorded is not None else ''}")
    return "\n".join(lines)


def cmd_rm(db, day: str) -> None:
    _parse_day(day)
    db.execute("DELETE FROM usage_readings WHERE day=?", (day,))
    db.commit()


def _db_from_argv(argv: list[str]) -> tuple[str, list[str]]:
    db_path = paths.default_db()
    rest = []
    i = 0
    while i < len(argv):
        item = argv[i]
        if item == "--db" and i + 1 < len(argv):
            db_path = argv[i + 1]
            i += 2
            continue
        if item.startswith("--db="):
            db_path = item.split("=", 1)[1]
            i += 1
            continue
        rest.append(item)
        i += 1
    return db_path, rest


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    db_path, rest = _db_from_argv(raw)
    ap = argparse.ArgumentParser(prog="quota.usage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="record a usage percentage for one day")
    p_add.add_argument("day", help="YYYY-MM-DD")
    p_add.add_argument("pct", type=float, help="usage percentage")
    p_add.add_argument("--note", default=None)
    sub.add_parser("list", help="list readings")
    p_rm = sub.add_parser("rm", help="delete one day's reading")
    p_rm.add_argument("day", help="YYYY-MM-DD")
    sub.add_parser("fit", help="regress usage deltas on activity")
    try:
        args = ap.parse_args(rest)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2
    try:
        if args.cmd == "add":
            _parse_day(args.day)
            if not math.isfinite(args.pct) or not 0 <= args.pct <= 100:
                print("usage percentage must be between 0 and 100", file=sys.stderr)
                return 2
        elif args.cmd == "rm":
            _parse_day(args.day)
    except ValueError:
        print(f"invalid day: {args.day}", file=sys.stderr)
        return 2
    try:
        db = open_db(db_path)
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.cmd == "add":
            cmd_add(db, args.day, args.pct, args.note)
        elif args.cmd == "list":
            text = cmd_list(db)
            if text:
                print(text)
        elif args.cmd == "rm":
            cmd_rm(db, args.day)
        elif args.cmd == "fit":
            print(fit_text(db))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

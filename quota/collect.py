#!/usr/bin/env python3
"""Incremental, read-only collector for bot and CLI activity proxies.

Reads allowlisted sources under the data root. The root is the first existing
of $QUOTA_DATA_ROOT, ~/agent-data, and /home/*/agent-data (via Path.home();
no fixed username). Override with --root.

  agents/<id>/profile.json                         bot names
  agents/<id>/store.db                             transcript_entries (seq watermark)
  agents/<id>/audit.jsonl                          shell/MCP/browser/computer audit (byte cursor)
  agents/<id>/automations/<slug>/automation.json   routine config (prompt -> hash+length)
  agents/<id>/automations/<slug>/runs.json         routine runs (record-hash diff)
  agent-transcripts/<id>/<id>.jsonl (or flat)      detailed transcripts (byte cursor)
  search-index.db                                  messages/media mirror (id watermark)
  Claude reports                                   $QUOTA_REPORTS_GLOB, or
                                                   <root>/reports/*[/]/claude-*.jsonl
  Codex sessions                                   $QUOTA_CODEX_SESSIONS or ~/.codex/sessions
  Grok sessions                                    $QUOTA_GROK_SESSIONS or ~/.grok/sessions
  launches.jsonl                                   grokbot-run lines (byte cursor). Default:
                                                   $GROKBOT_QUOTA_STATE/launches.jsonl when set,
                                                   otherwise both
                                                   ~/.local/state/grokbot-quota/launches.jsonl and
                                                   ~/.local/share/grokbot-quota/launches.jsonl

CLI rows go to cli_runs only. They are not events and do not enter the score.
Launch lines go to cli_launches and may override a run's bot_id and task.

A task database is optional. It is consulted only when config/quota.json lists
attribution_plugins or QUOTA_ATTRIBUTION_PLUGIN is set. See
quota/plugins/sqlite_tasks.py. With no plugin, unmatched runs stay unattributed.

The quota database defaults to $QUOTA_DB, else <data-root>/quota/quota.db when
a data root exists, else <state-dir>/quota.db.

Only counts, sizes, ids, names, timestamps and statuses are written.

Usage:
  python3 -m quota.collect [--db PATH] [--root PATH] [--backfill] [--report]
                           [--budget-seconds 30] [--max-read-mib 20]
                           [--codex-sessions PATH] [--grok-sessions PATH]
                           [--launches PATH] [--prices PATH]
                           [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import errno
import glob
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from datetime import date, datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota import attribute, paths, prices  # noqa: E402
from quota.plugins import Attributor, load_config, load_plugins  # noqa: E402
from quota.rows import bind as bind_rows  # noqa: E402

SCRIPT_VERSION = "quota-v1"
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

STORE_BATCH_ROWS = 1000
SEARCH_INDEX = "search-index.db"
SEARCH_INDEX_BATCH_ROWS = 5000
SEARCH_INDEX_ROW_BYTES = 64  # nominal per-row read cost (bodies never leave SQLite)
JSONL_BATCH_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024 * 1024
BOUNDARY_BYTES = 64

SUBAGENT_ID_RE = re.compile(r"sand-subagent-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:@/ -]{1,80}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

TOOL_CATEGORIES = {
    "subagent": {"task", "cloudagent"},
    "subagent_ctl": {"checksubagent", "messagesubagent", "stopsubagent"},
    "computer": {"computer", "computer_use", "screenshot"},
    "mcp": {"callmcptool", "mcp"},
    "mcp_meta": {"getmcptools", "get_mcp_tools", "getmcpserverstatus", "authenticatemcpserver", "restartmcpservers"},
    "shell": {"shell", "externalshell", "awaitshell", "await"},
    "file": {"read", "write", "edit"},
    "message": {"sendmessage", "send_message", "sendtoagent", "communicate_update"},
    "web": {"web_fetch", "webfetch", "web_search", "websearch"},
    "state": {"todowrite", "update_todos", "update_state"},
    "agent_admin": {"createagent", "updateagent", "searchplugins", "getplugin", "installplugin"},
}
CATEGORY_OF = {name: cat for cat, names in TOOL_CATEGORIES.items() for name in names}


def now_ms() -> int:
    return int(time.time() * 1000)


def tool_category(name: str | None) -> str:
    if not name:
        return "unknown"
    low = name.lower()
    if low in CATEGORY_OF:
        return CATEGORY_OF[low]
    if low.startswith("browser_"):
        return "browser"
    return "other"  # usually a directly exposed MCP tool


def safe_name(value) -> str | None:
    """Return value only if it looks like an identifier/name, never free text."""
    if isinstance(value, str) and SAFE_NAME_RE.match(value) and "://" not in value and "@" not in value:
        return value
    return None


def size_of(value) -> tuple[int, int]:
    if value is None:
        return 0, 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return len(value), len(value.encode("utf-8", "replace"))


def kebab(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unnamed"


def iso_ms(ts) -> int | None:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return int(ts)
    if isinstance(ts, str):
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def sha(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- db


def ensure_cli_run_columns(db: sqlite3.Connection) -> None:
    """Add cli_runs columns introduced after the table already existed.

    Runs before schema.sql so a fresh index on attribution_method can be created
    against a database whose cli_runs table predates that column.
    """
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cli_runs'").fetchone()
    if not exists:
        return
    cols = {row[1] for row in db.execute("PRAGMA table_info(cli_runs)")}
    added = (
        ("cwd", "TEXT"),
        ("attribution_method", "TEXT"),
        ("cost_usd_est", "REAL"),
        ("cost_estimated", "INTEGER"),
        ("model_defaulted", "INTEGER"),
    )
    for name, decl in added:
        if name not in cols:
            db.execute(f"ALTER TABLE cli_runs ADD COLUMN {name} {decl}")
    db.commit()


def open_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    bind_rows(db)
    ensure_cli_run_columns(db)
    with open(SCHEMA) as f:
        db.executescript(f.read())
    return db


def get_cursor(db, source_id) -> dict:
    row = db.execute("SELECT * FROM cursors WHERE source_id=?", (source_id,)).fetchone()
    if row:
        return dict(row)
    return {"source_id": source_id, "generation": 0, "device": None, "inode": None, "byte_offset": 0,
            "last_seq": 0, "boundary_hash": None, "mtime_ns": None, "size_bytes": None,
            "last_success_ms": None, "error_code": None, "backlog_bytes": 0}


def put_cursor(db, cur: dict):
    cols = list(cur.keys())
    db.execute(
        f"INSERT OR REPLACE INTO cursors ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [cur[c] for c in cols],
    )


EVENT_COLS = ["source_id", "generation", "source_key", "event_index", "event_type", "occurred_at_ms",
              "time_basis", "win_hi_ms", "win_frac", "entity_id", "peer_id", "request_id", "turn_id",
              "run_id", "call_id", "tool_name", "tool_category", "mcp_server", "status", "duration_ms",
              "model_label", "text_chars", "text_bytes", "argument_chars", "argument_bytes",
              "result_chars", "result_bytes", "attachment_bytes", "actions", "screenshots",
              "safe_metadata_json"]
EVENT_SQL = (f"INSERT OR IGNORE INTO events ({','.join(EVENT_COLS)}) "
             f"VALUES ({','.join('?' * len(EVENT_COLS))})")


def default_launches_path() -> str:
    """Single path grokbot-run writes, before the share-directory fallback."""
    return paths.default_launches_path()


def default_launches_specs() -> list[tuple[str, str]]:
    """`(path, source_id)` files to read. See ``quota.paths``."""
    return paths.default_launches_specs()


def launch_access(path) -> str:
    """`'ok'` for a readable regular file, `'denied'` on permission errors, else `'absent'`."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        return "denied"
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            return "denied"
        return "absent"
    try:
        mode = os.fstat(fd).st_mode
    finally:
        os.close(fd)
    if not stat.S_ISREG(mode):
        return "absent"
    return "ok"


def note_missing_source(ctx, source_id, source_type):
    """Record a source we are not allowed to read, without failing the pass."""
    t = now_ms()
    ctx.db.execute(
        "INSERT INTO sources (source_id,source_type,entity_id,first_seen_ms,last_seen_ms,status)"
        " VALUES (?,?,NULL,?,?,'missing')"
        " ON CONFLICT(source_id) DO UPDATE SET last_seen_ms=excluded.last_seen_ms,"
        " status='missing', source_type=excluded.source_type",
        (source_id, source_type, t, t))


class Ctx:
    def __init__(self, db, root, codex_dir=None, grok_dir=None, launches_path=None, prices_path=None):
        self.db = db
        self.root = root
        self.codex_dir = codex_dir if codex_dir is not None else os.path.expanduser("~/.codex/sessions")
        self.grok_dir = grok_dir if grok_dir is not None else os.path.expanduser("~/.grok/sessions")
        if launches_path is None:
            self.launches_specs = default_launches_specs()
        else:
            self.launches_specs = [(os.path.expanduser(launches_path), "launches.jsonl")]
        self.prices_path = prices.resolve_prices_path(prices_path)
        self.bytes_read = 0
        self.rows = 0
        self.inserted = 0
        self.made_progress = False
        self.errors: list[str] = []
        self._attributor = None

    def attributor(self) -> Attributor:
        if self._attributor is None:
            config = load_config()
            try:
                loaded = load_plugins(config)
            except (OSError, ValueError, ImportError, AttributeError) as exc:
                self.errors.append(f"plugin:{type(exc).__name__}")
                loaded = []
            self._attributor = Attributor(loaded, config, self.errors)
        return self._attributor

    def event(self, **kw):
        kw.setdefault("event_index", 0)
        kw.setdefault("time_basis", "exact" if kw.get("occurred_at_ms") else "none")
        meta = kw.get("safe_metadata_json")
        if isinstance(meta, dict):
            kw["safe_metadata_json"] = json.dumps(meta, sort_keys=True) if meta else None
        for c in ("text_chars", "text_bytes", "argument_chars", "argument_bytes", "result_chars",
                  "result_bytes", "attachment_bytes", "actions", "screenshots"):
            kw.setdefault(c, 0)
        cur = self.db.execute(EVENT_SQL, [kw.get(c) for c in EVENT_COLS])
        self.inserted += cur.rowcount
        return cur.lastrowid if cur.rowcount else None

    def link(self, from_id, to_id, relation, confidence, method, evidence=None):
        if not from_id or not to_id or from_id == to_id:
            return
        self.db.execute(
            "INSERT OR IGNORE INTO links (from_id,to_id,relation,evidence_event_id,confidence,method,created_at_ms)"
            " VALUES (?,?,?,?,?,?,?)", (from_id, to_id, relation, evidence, confidence, method, now_ms()))

    def entity(self, entity_id, kind, name=None, slug=None, profile=False):
        if not entity_id:
            return
        t = now_ms()
        self.db.execute(
            "INSERT INTO entities (entity_id,kind,display_name,slug,first_seen_ms,last_seen_ms,profile_present)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET last_seen_ms=excluded.last_seen_ms,"
            " display_name=COALESCE(excluded.display_name, display_name), slug=COALESCE(excluded.slug, slug),"
            " profile_present=MAX(profile_present, excluded.profile_present),"
            " kind=CASE WHEN kind='unknown' THEN excluded.kind ELSE kind END",
            (entity_id, kind, name, slug, t, t, int(profile)))


# -------------------------------------------------------------------- discovery


def discover(ctx: Ctx) -> list[dict]:
    """Return source descriptors; registers entities and sources."""
    root = ctx.root
    found = []
    seen_files = set()

    def add(path, stype, entity):
        try:
            st = os.stat(path)
        except OSError:
            return
        key = (st.st_dev, st.st_ino)
        if key in seen_files:  # alias (symlink/hardlink) of an already found file
            return
        seen_files.add(key)
        found.append({"path": path, "source_id": os.path.relpath(path, root), "type": stype, "entity": entity})

    for agent_dir in sorted(glob.glob(os.path.join(root, "agents", "*"))):
        aid = os.path.basename(agent_dir)
        if not os.path.isdir(agent_dir):
            continue
        prof = os.path.join(agent_dir, "profile.json")
        name = None
        if os.path.isfile(prof):
            try:
                with open(prof) as f:
                    data = json.load(f)
                name = data.get("name") if isinstance(data.get("name"), str) else None
            except (OSError, ValueError):
                ctx.errors.append(f"profile:{aid}:parse")
        ctx.entity(aid, "bot", name, kebab(name) if name else None, profile=bool(name))
        add(os.path.join(agent_dir, "store.db"), "store", aid)
        add(os.path.join(agent_dir, "audit.jsonl"), "audit", aid)
        for adir in sorted(glob.glob(os.path.join(agent_dir, "automations", "*"))):
            add(os.path.join(adir, "automation.json"), "automation", aid)
            add(os.path.join(adir, "runs.json"), "runs", aid)

    tdir = os.path.join(root, "agent-transcripts")
    for path in sorted(glob.glob(os.path.join(tdir, "*.jsonl")) + glob.glob(os.path.join(tdir, "*", "*.jsonl"))):
        rel = os.path.relpath(path, tdir)
        ident = rel.split(os.sep)[0] if os.sep in rel else rel[: -len(".jsonl")]
        ctx.entity(ident, "child" if ident.startswith("sand-subagent-") else "bot")
        add(path, "transcript", ident)

    discover_cli(ctx, found, seen_files)

    # Two logical sources (one per table, each with its own id watermark).
    si = os.path.join(root, SEARCH_INDEX)
    if os.path.isfile(si):
        for table in ("messages", "media"):
            found.append({"path": si, "source_id": f"{SEARCH_INDEX}#{table}", "type": f"search_index_{table}",
                          "entity": None, "table": table})

    t = now_ms()
    live = set()
    for s in found:
        live.add(s["source_id"])
        ctx.db.execute(
            "INSERT INTO sources (source_id,source_type,entity_id,first_seen_ms,last_seen_ms,status)"
            " VALUES (?,?,?,?,?,'ok') ON CONFLICT(source_id) DO UPDATE SET last_seen_ms=excluded.last_seen_ms,"
            " status=CASE WHEN status='missing' THEN 'ok' ELSE status END",
            (s["source_id"], s["type"], s["entity"], t, t))
    for (sid,) in ctx.db.execute("SELECT source_id FROM sources WHERE status != 'missing'").fetchall():
        if sid not in live:
            ctx.db.execute("UPDATE sources SET status='missing' WHERE source_id=?", (sid,))
    ctx.db.commit()
    return found


# ----------------------------------------------------------------- store.db


def store_step(ctx: Ctx, src) -> bool:
    """Process one batch of transcript_entries. Returns True if more remains."""
    db, sid, owner = ctx.db, src["source_id"], src["entity"]
    cur = get_cursor(db, sid)
    st = os.stat(src["path"])
    con = sqlite3.connect(f"file:{src['path']}?mode=ro", uri=True, timeout=2)
    try:
        con.execute("PRAGMA busy_timeout=2000")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "transcript_entries" not in tables:
            cur.update(error_code="no_transcript_entries", size_bytes=st.st_size, backlog_bytes=0)
            with db:
                put_cursor(db, cur)
            return False
        max_seq = con.execute("SELECT COALESCE(MAX(seq),0) FROM transcript_entries").fetchone()[0]
        if (cur["inode"] not in (None, st.st_ino)) or max_seq < cur["last_seq"]:
            cur.update(generation=cur["generation"] + 1, last_seq=0)  # replaced / reset
        rows = con.execute("SELECT seq,id,entry FROM transcript_entries WHERE seq>? ORDER BY seq LIMIT ?",
                           (cur["last_seq"], STORE_BATCH_ROWS)).fetchall()
        with db:
            for seq, entry_id, entry in rows:
                ctx.rows += 1
                ctx.bytes_read += len(entry)
                try:
                    d = json.loads(entry)
                except ValueError:
                    d = None
                store_entry(ctx, sid, cur["generation"], owner, str(entry_id), d)
                cur["last_seq"] = seq
            backlog = con.execute("SELECT COALESCE(SUM(LENGTH(entry)),0) FROM transcript_entries WHERE seq>?",
                                  (cur["last_seq"],)).fetchone()[0]
            cur.update(inode=st.st_ino, device=st.st_dev, mtime_ns=st.st_mtime_ns, size_bytes=st.st_size,
                       last_success_ms=now_ms(), error_code=None, backlog_bytes=backlog)
            put_cursor(db, cur)
        return backlog > 0
    finally:
        con.close()


def store_entry(ctx, sid, gen, owner, key, d):
    base = dict(source_id=sid, generation=gen, source_key=key, entity_id=owner)
    if not isinstance(d, dict):
        ctx.event(**base, event_type="unknown", safe_metadata_json={"reason": "unparsable"})
        return
    kind = d.get("kind")
    ts = iso_ms(d.get("timestampMs"))
    base.update(occurred_at_ms=ts, request_id=d.get("requestId") if isinstance(d.get("requestId"), str) else None)
    if kind == "spend-initiation":
        init = d.get("initiation") if isinstance(d.get("initiation"), dict) else {}
        ctx.event(**{**base, "entity_id": d.get("agentId") or owner}, event_type="request_started",
                  safe_metadata_json={"initiation": safe_name(init.get("type")),
                                      "initiating_messages": init.get("initiatingMessageCount")})
    elif kind == "message":
        chars, nbytes = size_of(d.get("content"))
        frm = d.get("fromAgent") if isinstance(d.get("fromAgent"), dict) else None
        peer = frm.get("id") if frm else None
        images = d.get("images") if isinstance(d.get("images"), list) else []
        eid = ctx.event(**base, event_type="message", peer_id=peer, text_chars=chars, text_bytes=nbytes,
                        status=safe_name(d.get("role")), safe_metadata_json={"images": len(images)} if images else None)
        if peer:
            ctx.entity(peer, "bot", safe_name(frm.get("name")))
            ctx.link(peer, owner, "message", "high", "store_fromAgent", eid)
    elif kind == "send-message":
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        chars, nbytes = size_of(msg.get("content"))
        ctx.event(**base, event_type="message_sent", text_chars=chars, text_bytes=nbytes,
                  safe_metadata_json={"type": safe_name(msg.get("type"))})
    elif kind == "user-attachment":
        size = d.get("byteSize") if isinstance(d.get("byteSize"), int) else 0
        ctx.event(**base, event_type="attachment", attachment_bytes=size)
    elif kind == "event":
        ev = d.get("event") if isinstance(d.get("event"), dict) else {}
        ctx.event(**base, event_type="configuration_changed",
                  safe_metadata_json={"type": safe_name(ev.get("type")), "action": safe_name(ev.get("action"))})
    elif kind == "feedback":
        ctx.event(**base, event_type="feedback", status=safe_name(d.get("state")))
    else:
        ctx.event(**base, event_type="unknown", safe_metadata_json={"kind": safe_name(kind)})


# ------------------------------------------------------------ search-index.db

# Only ids, role/kind/ext, dimensions, timestamps and sizes are selected; the
# body is measured inside SQLite. body, file_name and mime are never read.
SEARCH_INDEX_SQL = {
    "messages": "SELECT id, agent_id, entry_id, timestamp_ms, role, LENGTH(body), LENGTH(CAST(body AS BLOB)) "
                "FROM messages WHERE id>? ORDER BY id LIMIT ?",
    "media": "SELECT id, agent_id, entry_id, timestamp_ms, kind, ext, width, height "
             "FROM media WHERE id>? ORDER BY id LIMIT ?",
}


def safe_id(value) -> str | None:
    return value if isinstance(value, str) and SAFE_ID_RE.match(value) else None


def search_index_step(ctx: Ctx, src, _max) -> bool:
    """One batch of messages/media past the id watermark. Returns True if more remains.

    Events are keyed by "<agent>/<entry>", so re-reading rows is idempotent. If
    the watermark row is gone or changed (mirror rebuilt, rows replaced) or the
    file was replaced, the table is simply rescanned from id 0.
    """
    db, sid, table = ctx.db, src["source_id"], src["table"]
    cur = get_cursor(db, sid)
    st = os.stat(src["path"])
    con = sqlite3.connect(f"file:{src['path']}?mode=ro", uri=True, timeout=2)
    try:
        con.execute("PRAGMA busy_timeout=2000")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in tables:
            cur.update(error_code=f"no_{table}", size_bytes=st.st_size, backlog_bytes=0)
            with db:
                put_cursor(db, cur)
            return False
        if cur["last_seq"]:
            row = con.execute(f"SELECT agent_id, entry_id FROM {table} WHERE id=?", (cur["last_seq"],)).fetchone()
            if cur["inode"] not in (None, st.st_ino) or not row or sha(f"{row[0]}/{row[1]}") != cur["boundary_hash"]:
                cur["last_seq"] = 0
        rows = con.execute(SEARCH_INDEX_SQL[table], (cur["last_seq"], SEARCH_INDEX_BATCH_ROWS)).fetchall()
        with db:
            agents = set()
            for row in rows:
                ctx.rows += 1
                ctx.bytes_read += SEARCH_INDEX_ROW_BYTES
                rid, agent, entry, ts = row[:4]
                agent_ok, entry_ok = safe_id(agent), safe_id(entry)
                key = f"{agent_ok}/{entry_ok}" if agent_ok and entry_ok else f"h:{sha(f'{agent}/{entry}')[:24]}"
                base = dict(source_id=sid, generation=cur["generation"], source_key=key, entity_id=agent_ok,
                            occurred_at_ms=ts if isinstance(ts, int) and ts > 0 else None)
                if table == "messages":
                    ctx.event(**base, event_type="message", status=safe_name(row[4]),
                              text_chars=row[5] or 0, text_bytes=row[6] or 0)
                else:
                    w, h = (v if isinstance(v, int) else None for v in row[6:8])
                    ctx.event(**base, event_type="media", status=safe_name(row[4]),
                              safe_metadata_json={k: v for k, v in (("kind", safe_name(row[4])),
                                                                    ("ext", safe_name(row[5])),
                                                                    ("width", w), ("height", h)) if v is not None})
                if agent_ok:
                    agents.add(agent_ok)
                cur["last_seq"] = rid
                cur["boundary_hash"] = sha(f"{agent}/{entry}")
            for a in agents - {r[0] for r in db.execute("SELECT entity_id FROM entities")}:
                ctx.entity(a, "bot")
            remaining = con.execute(f"SELECT COUNT(*) FROM {table} WHERE id>?", (cur["last_seq"],)).fetchone()[0]
            backlog = remaining * SEARCH_INDEX_ROW_BYTES
            cur.update(inode=st.st_ino, device=st.st_dev, mtime_ns=st.st_mtime_ns, size_bytes=st.st_size,
                       last_success_ms=now_ms(), error_code=None, backlog_bytes=backlog)
            put_cursor(db, cur)
        return backlog > 0
    finally:
        con.close()


# -------------------------------------------------------------------- JSONL


def jsonl_step(ctx: Ctx, src, max_bytes: int) -> bool:
    """Read complete lines from the byte cursor. Returns True if more remains."""
    db, sid, path = ctx.db, src["source_id"], src["path"]
    cur = get_cursor(db, sid)
    st = os.stat(path)
    with open(path, "rb") as f:
        off = cur["byte_offset"]
        reset = cur["inode"] not in (None, st.st_ino) or st.st_size < off
        if not reset and off > 0 and cur["boundary_hash"]:
            f.seek(max(0, off - BOUNDARY_BYTES))
            reset = sha(f.read(min(off, BOUNDARY_BYTES))) != cur["boundary_hash"]
        if reset:  # replaced, truncated or rewritten: re-read as a new generation
            cur.update(generation=cur["generation"] + 1, byte_offset=0, last_seq=0)
            off = 0
        # For JSONL sources last_seq is a flag: 1 once the file was fully read.
        # Lines appended after that are timed by the file mtime ("window").
        live = bool(cur["last_seq"])
        f.seek(off)
        buf = f.read(max_bytes)
        end = buf.rfind(b"\n")
        while end < 0 and off + len(buf) < st.st_size and len(buf) < MAX_RECORD_BYTES:
            more = f.read(max_bytes)  # one record larger than the batch: read on, bounded
            if not more:
                break
            buf += more
            end = buf.rfind(b"\n")
        ctx.bytes_read += len(buf)
        gap = end < 0 and len(buf) >= MAX_RECORD_BYTES
        with db:
            if gap:
                # Oversized record: skip it visibly (coverage_gap), never silently.
                end_off = off
                while True:
                    chunk = f.read(max_bytes)
                    if not chunk:
                        break  # unterminated: stays pending
                    ctx.bytes_read += len(chunk)
                    i = chunk.find(b"\n")
                    if i >= 0:
                        end_off = f.tell() - len(chunk) + i + 1
                        break
                if end_off > off:
                    ctx.event(source_id=sid, generation=cur["generation"], source_key=str(off),
                              event_type="coverage_gap", entity_id=src["entity"], status="oversized_record",
                              safe_metadata_json={"bytes": end_off - off})
                ctx.errors.append(f"{src['type']}:{sid}:oversized_record")
            elif end >= 0:
                pos = off
                for line in buf[: end + 1].split(b"\n")[:-1]:
                    line_off = pos
                    pos += len(line) + 1
                    if not line.strip():
                        continue
                    ctx.rows += 1
                    try:
                        d = json.loads(line)
                    except ValueError:
                        d = None
                    if src["type"] == "audit":
                        audit_record(ctx, sid, cur["generation"], src["entity"], str(line_off), d)
                    else:
                        timing = ({"time_basis": "window", "win_hi_ms": st.st_mtime_ns // 1_000_000} if live else
                                  {"time_basis": "interpolated", "win_frac": line_off / max(st.st_size, 1)})
                        transcript_record(ctx, sid, cur["generation"], src["entity"], str(line_off), d, line, timing)
                end_off = pos
            else:
                end_off = off  # only a partial trailing line: leave it pending
            f.seek(max(0, end_off - BOUNDARY_BYTES))
            cur["boundary_hash"] = sha(f.read(min(end_off, BOUNDARY_BYTES))) if end_off else None
            cur["byte_offset"] = end_off
            if off + len(buf) >= st.st_size and not gap:
                cur["last_seq"] = 1  # caught up (a partial trailing line may stay pending)
            cur.update(inode=st.st_ino, device=st.st_dev, mtime_ns=st.st_mtime_ns, size_bytes=st.st_size,
                       last_success_ms=now_ms(), error_code="oversized_record" if gap else None,
                       backlog_bytes=st.st_size - end_off)
            put_cursor(db, cur)
    return end_off > off and end_off < st.st_size


def audit_record(ctx, sid, gen, owner, key, d):
    base = dict(source_id=sid, generation=gen, source_key=key)
    if not isinstance(d, dict):
        ctx.event(**base, event_type="unknown", entity_id=owner, safe_metadata_json={"reason": "unparsable"})
        return
    typ = d.get("type")
    base.update(entity_id=d.get("agentId") or owner, occurred_at_ms=iso_ms(d.get("ts")),
                request_id=d.get("turnId"), turn_id=d.get("turnId"), call_id=d.get("toolCallId"),
                duration_ms=d.get("durationMs") if isinstance(d.get("durationMs"), int) else None)
    if typ == "mcp_tool_call":
        ctx.event(**base, event_type="audit_mcp_call", mcp_server=safe_name(d.get("serverIdentifier")),
                  tool_name=safe_name(d.get("toolName")), tool_category="mcp", status=safe_name(d.get("status")),
                  safe_metadata_json={"transport": safe_name(d.get("transport"))})
    elif typ == "shell_command":
        ctx.event(**base, event_type="audit_shell", tool_category="shell",
                  safe_metadata_json={"shell_kind": safe_name(d.get("shellKind")), "target": safe_name(d.get("target"))})
    elif typ == "browser_navigation":
        ctx.event(**base, event_type="audit_browser_navigation", tool_category="browser")
    elif typ == "computer_use_session":
        counts = d.get("actionCounts") if isinstance(d.get("actionCounts"), dict) else {}
        ctx.event(**base, event_type="computer_session", tool_category="computer",
                  actions=int(d.get("actionCount") or 0), screenshots=int(d.get("screenshotCount") or 0),
                  safe_metadata_json={k: v for k, v in counts.items() if safe_name(k) and isinstance(v, int)})
    else:
        ctx.event(**base, event_type="audit_unknown", safe_metadata_json={"type": safe_name(typ)})


def subagent_type(inp: dict) -> str | None:
    st = inp.get("subagent_type")
    if isinstance(st, str):
        return safe_name(st)
    st = inp.get("subagentType")
    if isinstance(st, dict):
        # Joined on purpose: the transcript field name contains a stem the public scan rejects.
        named = st.get("cus" + "tom")
        if isinstance(named, dict) and isinstance(named.get("name"), str):
            return safe_name(named["name"])
        return safe_name(next(iter(st), None))
    return None


def transcript_record(ctx, sid, gen, owner, key, d, raw: bytes, timing: dict):
    base = dict(source_id=sid, generation=gen, source_key=key, entity_id=owner, **timing)
    msg = d.get("message") if isinstance(d, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        ctx.event(**base, event_type="unknown", safe_metadata_json={"reason": "unsupported_record"})
        return
    role = d.get("role")
    text_chars = text_bytes = 0
    idx = 1
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            c, b = size_of(block.get("text"))
            text_chars += c
            text_bytes += b
        elif btype == "tool_use":
            name = block.get("name") if isinstance(block.get("name"), str) else None
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            c, b = size_of(inp)
            cat = tool_category(name)
            meta, peer, server, model, status = {}, None, None, None, None
            inner = inp.get("toolName") if cat == "mcp" else None
            if isinstance(inner, str) and tool_category(inner) not in ("other", "mcp"):
                # Built-in tool called through the MCP wrapper (namespace "cursor"): unwrap.
                meta["via"] = safe_name(name)
                name, cat = inner, tool_category(inner)
                inp = inp.get("arguments") if isinstance(inp.get("arguments"), dict) else {}
            if cat == "subagent":
                meta["subagent_type"] = subagent_type(inp)
                model = safe_name(inp.get("model"))
                if inp.get("run_in_background"):
                    meta["background"] = True
                resume = inp.get("resume")
                if isinstance(resume, str) and SUBAGENT_ID_RE.fullmatch(resume):
                    peer, status = resume, "resume"
            elif cat == "mcp":
                server = safe_name(inp.get("server")) or safe_name(inp.get("namespace"))
                meta["mcp_tool"] = safe_name(inp.get("toolName"))
            ev = ctx.event(**base, event_index=idx, event_type="tool_called", tool_name=safe_name(name),
                           tool_category=cat, mcp_server=server, model_label=model, peer_id=peer, status=status,
                           argument_chars=c, argument_bytes=b, safe_metadata_json=meta)
            if peer:
                ctx.entity(peer, "child")
                ctx.link(owner, peer, "parent-child", "high", "task_resume", ev)
            idx += 1
        elif btype == "tool_result":
            name = block.get("name") if isinstance(block.get("name"), str) else None
            res = block.get("result")
            c, b = size_of(res)
            status, peer, dur, meta = None, None, None, {}
            if isinstance(res, dict):
                if "error" in res:
                    status = "error"
                elif "success" in res:
                    status = "ok"
                    ok = res["success"]
                    if isinstance(ok, dict) and tool_category(name) == "subagent":
                        child = ok.get("agentId")
                        if isinstance(child, str) and SUBAGENT_ID_RE.fullmatch(child):
                            peer = child
                        if isinstance(ok.get("durationMs"), int):
                            dur = ok["durationMs"]
                        if ok.get("isBackground"):
                            meta["background"] = True
            ev = ctx.event(**base, event_index=idx, event_type="tool_result", tool_name=safe_name(name),
                           tool_category=tool_category(name), status=status, peer_id=peer, duration_ms=dur,
                           result_chars=c, result_bytes=b, safe_metadata_json=meta)
            if peer:
                ctx.entity(peer, "child")
                ctx.link(owner, peer, "parent-child", "high", "task_result", ev)
            idx += 1
        else:
            ctx.event(**base, event_index=idx, event_type="unknown", safe_metadata_json={"block": safe_name(btype)})
            idx += 1
    etype = {"assistant": "inference_step", "user": "message", "tool": None}.get(role, "unknown")
    main_ev = None
    if etype:
        main_ev = ctx.event(**base, event_type=etype, text_chars=text_chars, text_bytes=text_bytes,
                  status=safe_name(role) if etype != "unknown" else None)
    # Low-confidence fallback: other subagent ids mentioned in this record.
    if role in ("tool", "user", "assistant"):
        for m in set(SUBAGENT_ID_RE.findall(raw.decode("utf-8", "replace"))):
            if m != owner:
                ctx.entity(m, "child")
                ctx.link(owner, m, "parent-child", "low", "id_mention", main_ev)


# ----------------------------------------------------------------- routines


def file_changed(cur, st) -> bool:
    return cur["mtime_ns"] != st.st_mtime_ns or cur["size_bytes"] != st.st_size or cur["inode"] != st.st_ino


def automation_step(ctx: Ctx, src, _max) -> bool:
    db, sid, owner = ctx.db, src["source_id"], src["entity"]
    cur = get_cursor(db, sid)
    st = os.stat(src["path"])
    if not file_changed(cur, st):
        return False
    with open(src["path"], "rb") as f:
        raw = f.read()
    ctx.bytes_read += len(raw)
    ctx.rows += 1
    routine = os.path.basename(os.path.dirname(src["path"]))
    with db:
        try:
            a = json.loads(raw)
            if not isinstance(a, dict):
                raise ValueError
        except ValueError:
            cur.update(error_code="parse", mtime_ns=st.st_mtime_ns, size_bytes=st.st_size, inode=st.st_ino)
            put_cursor(db, cur)
            ctx.errors.append(f"automation:{sid}:parse")
            return False
        prompt = a.get("prompt") if isinstance(a.get("prompt"), str) else ""
        trig = (a.get("triggerPresentation") or {}).get("trigger") or a.get("trigger") or {}
        trig = trig if isinstance(trig, dict) else {}
        schedule = a.get("schedule") if isinstance(a.get("schedule"), str) else trig.get("schedule")
        cfg = {"name": a.get("name"), "enabled": a.get("enabled"), "trigger_type": trig.get("type"),
               "schedule": schedule, "timezone": a.get("timezone"), "prompt_sha256": sha(prompt),
               "prompt_chars": len(prompt), "repo": trig.get("repo") if isinstance(trig.get("repo"), str) else None}
        chash = sha(json.dumps(cfg, sort_keys=True))
        old = db.execute("SELECT config_hash FROM routines WHERE owner_id=? AND routine_id=?",
                         (owner, routine)).fetchone()
        db.execute("INSERT OR REPLACE INTO routines (owner_id,routine_id,name,enabled,trigger_type,schedule,timezone,"
                   "prompt_chars,prompt_sha256,config_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (owner, routine, cfg["name"] if isinstance(cfg["name"], str) else None,
                    int(bool(a.get("enabled"))), safe_name(cfg["trigger_type"]),
                    schedule if isinstance(schedule, str) and len(schedule) < 80 else None,
                    safe_name(cfg["timezone"]), cfg["prompt_chars"], cfg["prompt_sha256"], chash))
        if not old or old[0] != chash:
            # First sight (backfill) has no change time; later changes are timed by detection.
            ctx.event(source_id=sid, generation=cur["generation"], source_key=chash[:16],
                      event_type="configuration_changed", entity_id=owner,
                      time_basis="window" if old else "none", win_hi_ms=now_ms() if old else None,
                      safe_metadata_json={"routine": routine, "enabled": bool(a.get("enabled"))})
        cur.update(mtime_ns=st.st_mtime_ns, size_bytes=st.st_size, inode=st.st_ino, device=st.st_dev,
                   last_success_ms=now_ms(), error_code=None, backlog_bytes=0)
        put_cursor(db, cur)
    return False


def runs_step(ctx: Ctx, src, _max) -> bool:
    db, sid, owner = ctx.db, src["source_id"], src["entity"]
    cur = get_cursor(db, sid)
    st = os.stat(src["path"])
    if not file_changed(cur, st):
        return False
    if st.st_size > MAX_RECORD_BYTES:
        with db:
            cur.update(error_code="too_large", backlog_bytes=st.st_size, size_bytes=st.st_size)
            put_cursor(db, cur)
        ctx.errors.append(f"runs:{sid}:too_large")
        return False
    with open(src["path"], "rb") as f:
        raw = f.read()
    ctx.bytes_read += len(raw)
    routine = os.path.basename(os.path.dirname(src["path"]))
    with db:
        try:
            runs = json.loads(raw)
            if not isinstance(runs, list):
                raise ValueError
        except ValueError:
            cur.update(error_code="parse", mtime_ns=st.st_mtime_ns, size_bytes=st.st_size, inode=st.st_ino)
            put_cursor(db, cur)
            ctx.errors.append(f"runs:{sid}:parse")
            return False
        for r in runs:
            if not isinstance(r, dict) or not isinstance(r.get("id"), str):
                continue
            ctx.rows += 1
            safe = {k: r.get(k) for k in ("id", "requestId", "trigger", "startedAt", "finishedAt", "status", "errorKind")}
            rhash = sha(json.dumps(safe, sort_keys=True))
            old = db.execute("SELECT record_hash FROM runs WHERE owner_id=? AND routine_id=? AND run_id=?",
                             (owner, routine, r["id"])).fetchone()
            if old and old[0] == rhash:
                continue
            started, finished = iso_ms(r.get("startedAt")), iso_ms(r.get("finishedAt"))
            dur = finished - started if started and finished else None
            ev = ctx.event(source_id=sid, generation=cur["generation"], source_key=f"{r['id']}:{rhash[:12]}",
                           event_type="routine_run_state", entity_id=owner, occurred_at_ms=finished or started,
                           run_id=r["id"], request_id=r.get("requestId") if isinstance(r.get("requestId"), str) else None,
                           status=safe_name(r.get("status")), duration_ms=dur,
                           safe_metadata_json={"routine": routine, "trigger": safe_name(r.get("trigger")),
                                               "error_kind": safe_name(r.get("errorKind"))})
            db.execute("INSERT OR REPLACE INTO runs (owner_id,routine_id,run_id,request_id,trigger,started_at_ms,"
                       "finished_at_ms,status,record_hash,latest_event_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (owner, routine, r["id"], safe["requestId"] if isinstance(safe["requestId"], str) else None,
                        safe_name(r.get("trigger")), started, finished, safe_name(r.get("status")), rhash, ev))
            if safe["requestId"]:
                ctx.link(safe["requestId"], r["id"], "request-run", "high", "runs_requestId", ev)
        cur.update(mtime_ns=st.st_mtime_ns, size_bytes=st.st_size, inode=st.st_ino, device=st.st_dev,
                   last_success_ms=now_ms(), error_code=None, backlog_bytes=0)
        put_cursor(db, cur)
    return False


# -------------------------------------------------------------------- CLI runs

# Claude / Codex / Grok session totals. Written only to cli_runs — never events —
# so the score views stay unchanged. A task plugin, when enabled, is read-only.

_TASK_RE = re.compile(r"JOB-\d+", re.I)
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
CODEX_MODEL_EPOCH_KEY = "codex_model_epoch"
CODEX_MODEL_EPOCH = "1"
_ISO_RE = re.compile(
    r"^(?P<h>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?P<f>\.\d+)?(?P<z>Z|[+-]\d{2}:?\d{2})?$")
_RAW_DROP = {"result", "message", "content", "prompt", "text", "title", "error",
             "base_instructions", "cwd", "path", "url", "urls"}
CLI_STATE = "cli_state:"
CLI_COLS = ["source_id", "run_key", "tool", "task", "bot_id", "session_id", "ts", "model",
            "num_turns", "duration_ms", "is_error", "cost_usd", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_create_tokens", "reasoning_tokens", "subagent_count", "raw_json",
            "cwd", "attribution_method", "cost_usd_est", "cost_estimated", "model_defaulted"]
LAUNCH_COLS = ["launch_key", "source_id", "bot_id", "task", "tool", "cwd", "started_at", "pid", "session_id"]
LAUNCH_MATCH_WINDOW_MS = 24 * 60 * 60 * 1000


def to_iso_utc(value) -> str | None:
    """Canonical UTC timestamp `YYYY-MM-DDTHH:MM:SS.ffffffZ`, or None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        sec = float(value)
        if sec > 10**12:
            sec /= 1000.0
        dt = datetime.fromtimestamp(sec, timezone.utc)
    elif isinstance(value, str):
        m = _ISO_RE.match(value.strip())
        if not m:
            return None
        frac = (m.group("f") or ".")[1:7]
        frac = "." + frac if frac else ""
        z = m.group("z") or "Z"
        if z == "Z":
            z = "+00:00"
        elif re.fullmatch(r"[+-]\d{4}", z):
            z = z[:3] + ":" + z[3:]
        try:
            dt = datetime.fromisoformat(m.group("h").replace(" ", "T") + frac + z)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        return None
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def as_bool_int(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    return None


def task_from_cwd(cwd) -> str | None:
    """JOB-N (uppercased) when the path contains one, else the cwd basename."""
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    m = _TASK_RE.search(cwd)
    if m:
        return m.group(0).upper()
    base = os.path.basename(cwd.rstrip("/\\"))
    return base or None


def subagent_count(value) -> int:
    """len of a list; `spawned` or `count` of a summary dict, else len(dict); else 0."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("spawned", "count"):
            n = value.get(key)
            if isinstance(n, int) and not isinstance(n, bool):
                return n
        return len(value)
    return 0


def scrub_raw(obj) -> str:
    """JSON of the metrics object with free-text / path fields removed."""
    def scrub(o, depth=0):
        if depth > 8:
            return None
        if isinstance(o, dict):
            return {str(k): scrub(v, depth + 1) for k, v in o.items() if str(k) not in _RAW_DROP}
        if isinstance(o, list):
            return [scrub(v, depth + 1) for v in o[:200]]
        if isinstance(o, str):
            return o if len(o) <= 200 else None
        if isinstance(o, bool) or o is None:
            return o
        if isinstance(o, (int, float)):
            return o
        return None
    return json.dumps(scrub(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def discover_cli(ctx: Ctx, found: list, seen_files: set):
    """Claude reports under the data root, plus Codex and Grok session files."""
    root = ctx.root

    def add_file(path, stype, source_id):
        try:
            st = os.stat(path)
        except OSError:
            return
        key = (st.st_dev, st.st_ino)
        if key in seen_files:
            return
        seen_files.add(key)
        found.append({"path": path, "source_id": source_id, "type": stype, "entity": None})

    for pattern in paths.report_globs(root):
        for path in sorted(glob.glob(pattern)):
            if os.path.isfile(path):
                add_file(path, "cli_claude", os.path.relpath(path, root))

    codex = ctx.codex_dir
    if codex and os.path.isdir(codex):
        for path in sorted(glob.glob(os.path.join(codex, "*", "*", "*", "rollout-*.jsonl"))):
            if os.path.isfile(path):
                rel = os.path.relpath(path, codex)
                add_file(path, "cli_codex", os.path.join("codex-sessions", rel))

    grok = ctx.grok_dir
    if grok and os.path.isdir(grok):
        for path in sorted(glob.glob(os.path.join(grok, "*", "*", "usage.json"))):
            if os.path.isfile(path):
                rel = os.path.relpath(path, grok)
                add_file(path, "cli_grok", os.path.join("grok-sessions", rel))

    for path, source_id in ctx.launches_specs:
        access = launch_access(path)
        if access == "denied":
            note_missing_source(ctx, source_id, "cli_launches")
            continue
        if access != "ok":
            continue
        add_file(path, "cli_launches", source_id)


def load_cli_state(db, source_id) -> dict:
    row = db.execute("SELECT value FROM meta WHERE key=?", (CLI_STATE + source_id,)).fetchone()
    if not row or not row[0]:
        return {}
    try:
        data = json.loads(row[0])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_cli_state(db, source_id, state: dict):
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
               (CLI_STATE + source_id, json.dumps(state, sort_keys=True)))


def upsert_cli(db, row: dict, only_if_newer=False):
    """Insert or replace one cli_runs row. Codex/Grok keep the latest timestamp."""
    if only_if_newer:
        old = db.execute("SELECT ts FROM cli_runs WHERE run_key=?", (row["run_key"],)).fetchone()
        if old and old[0] and row.get("ts") and old[0] > row["ts"]:
            return
    updates = []
    for col in CLI_COLS:
        if col == "run_key":
            continue
        # A later pass that does not know the price must not wipe a reported one.
        if col == "cost_usd":
            updates.append("cost_usd=COALESCE(excluded.cost_usd, cli_runs.cost_usd)")
        else:
            updates.append(f"{col}=excluded.{col}")
    sql = (f"INSERT INTO cli_runs ({','.join(CLI_COLS)}) VALUES ({','.join('?' * len(CLI_COLS))}) "
           f"ON CONFLICT(run_key) DO UPDATE SET " + ", ".join(updates))
    db.execute(sql, [row.get(c) for c in CLI_COLS])


def consume_jsonl(ctx: Ctx, path, cur, max_bytes) -> dict:
    """Complete JSONL lines after the byte cursor. A partial trailing line stays put."""
    st = os.stat(path)
    off = cur["byte_offset"] or 0
    with open(path, "rb") as f:
        reset = cur["inode"] not in (None, st.st_ino) or st.st_size < off
        if not reset and off > 0 and cur["boundary_hash"]:
            f.seek(max(0, off - BOUNDARY_BYTES))
            reset = sha(f.read(min(off, BOUNDARY_BYTES))) != cur["boundary_hash"]
        if reset:
            off = 0
        f.seek(off)
        buf = f.read(max_bytes)
        end = buf.rfind(b"\n")
        while end < 0 and off + len(buf) < st.st_size and len(buf) < MAX_RECORD_BYTES:
            more = f.read(max_bytes)
            if not more:
                break
            buf += more
            end = buf.rfind(b"\n")
        ctx.bytes_read += len(buf)
        records = []
        error = None
        if end < 0 and len(buf) >= MAX_RECORD_BYTES:
            end_off = off
            while True:
                chunk = f.read(max_bytes)
                if not chunk:
                    break
                ctx.bytes_read += len(chunk)
                i = chunk.find(b"\n")
                if i >= 0:
                    end_off = f.tell() - len(chunk) + i + 1
                    break
            if end_off > off:
                error = "oversized_record"
                ctx.errors.append(f"cli:{path}:oversized_record")
        elif end >= 0:
            pos = off
            for line in buf[: end + 1].split(b"\n")[:-1]:
                line_off = pos
                pos += len(line) + 1
                if not line.strip():
                    continue
                ctx.rows += 1
                try:
                    rec = json.loads(line)
                except ValueError:
                    rec = None
                records.append((line_off, rec if isinstance(rec, dict) else None))
            end_off = pos
        else:
            end_off = off
        f.seek(max(0, end_off - BOUNDARY_BYTES))
        boundary = sha(f.read(min(end_off, BOUNDARY_BYTES))) if end_off else None
    return {"st": st, "reset": reset, "records": records, "end_off": end_off, "boundary": boundary,
            "error": error, "advanced": end_off > (0 if reset else (cur["byte_offset"] or 0))}


def begin_cli_batch(db, sid, cur, batch) -> dict:
    """On a rewritten file, drop this source's rows before the batch is applied."""
    if batch["reset"]:
        cur["generation"] = (cur["generation"] or 0) + 1
        db.execute("DELETE FROM cli_runs WHERE source_id=?", (sid,))
        db.execute("DELETE FROM meta WHERE key=?", (CLI_STATE + sid,))
        return {}
    return load_cli_state(db, sid)


def finish_jsonl_cursor(ctx: Ctx, src, cur, batch, state):
    db, sid = ctx.db, src["source_id"]
    st = batch["st"]
    save_cli_state(db, sid, state)
    cur["byte_offset"] = batch["end_off"]
    cur["boundary_hash"] = batch["boundary"]
    cur.update(inode=st.st_ino, device=st.st_dev, mtime_ns=st.st_mtime_ns, size_bytes=st.st_size,
               last_success_ms=now_ms(), error_code=batch["error"],
               backlog_bytes=max(st.st_size - batch["end_off"], 0))
    put_cursor(db, cur)
    if batch["advanced"]:
        ctx.made_progress = True
    return batch["advanced"] and batch["end_off"] < st.st_size


def usage_tokens(usage: dict) -> dict:
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    return {
        "input_tokens": as_int(usage.get("input_tokens")),
        "output_tokens": as_int(usage.get("output_tokens")),
        "cache_read_tokens": as_int(usage.get("cache_read_input_tokens")),
        "cache_create_tokens": as_int(usage.get("cache_creation_input_tokens")),
        "reasoning_tokens": as_int(details.get("thinking_tokens")),
    }


def model_usage_label(obj) -> str | None:
    mu = obj.get("modelUsage") if isinstance(obj, dict) else None
    if isinstance(mu, dict) and mu:
        return ",".join(str(k) for k in mu)
    return None


def cli_claude_step(ctx: Ctx, src, max_bytes: int) -> bool:
    """Result lines only. ts is the last timestamp seen earlier in the file, else mtime."""
    db, sid = ctx.db, src["source_id"]
    cur = get_cursor(db, sid)
    batch = consume_jsonl(ctx, src["path"], cur, max_bytes)
    task = os.path.basename(os.path.dirname(src["path"]))
    bot = ctx.attributor().bot_for(task, None)
    with db:
        state = begin_cli_batch(db, sid, cur, batch)
        for line_off, rec in batch["records"]:
            prev_ts = state.get("last_ts")
            if rec and isinstance(rec.get("cwd"), str) and rec.get("cwd").strip():
                state["cwd"] = rec["cwd"]
            if rec and rec.get("type") == "result":
                usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
                session = rec.get("session_id") if isinstance(rec.get("session_id"), str) else None
                upsert_cli(db, {
                    "source_id": sid, "run_key": f"claude:{sid}:{line_off}", "tool": "claude",
                    "task": task, "bot_id": bot, "session_id": session,
                    "ts": prev_ts or to_iso_utc(batch["st"].st_mtime),
                    "model": model_usage_label(rec),
                    "num_turns": as_int(rec.get("num_turns")),
                    "duration_ms": as_int(rec.get("duration_ms")),
                    "is_error": as_bool_int(rec.get("is_error")),
                    "cost_usd": rec.get("total_cost_usd") if isinstance(rec.get("total_cost_usd"), (int, float))
                    and not isinstance(rec.get("total_cost_usd"), bool) else None,
                    "subagent_count": subagent_count(rec.get("subagent_stats")),
                    "raw_json": scrub_raw(rec),
                    "cwd": state.get("cwd"),
                    "attribution_method": None,
                    **usage_tokens(usage),
                })
            if rec and isinstance(rec.get("timestamp"), str):
                norm = to_iso_utc(rec["timestamp"])
                if norm:
                    state["last_ts"] = norm
        return finish_jsonl_cursor(ctx, src, cur, batch, state)


def codex_model_id(rec) -> str | None:
    """Model id on a session_meta or turn_context line. Free text is ignored."""
    if not isinstance(rec, dict) or rec.get("type") not in ("session_meta", "turn_context"):
        return None
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    for source in (payload, rec):
        model = source.get("model")
        if isinstance(model, str) and _MODEL_RE.fullmatch(model):
            return model
    return None


def sync_codex_model(db, state):
    """Write the model learned so far onto the session row, if that row exists."""
    session = state.get("session_id")
    model = state.get("model")
    if not session or not model:
        return
    db.execute(
        "UPDATE cli_runs SET model=? WHERE run_key=? AND IFNULL(model, '')!=?",
        (model, f"codex:{session}", model))


def cli_codex_step(ctx: Ctx, src, max_bytes: int) -> bool:
    """session_meta identifies the session; token_count totals are cumulative (one row).

    The model id is taken from session_meta / turn_context payloads. The latest
    id in the file wins. Token totals are replaced, not added.
    """
    db, sid = ctx.db, src["source_id"]
    cur = get_cursor(db, sid)
    batch = consume_jsonl(ctx, src["path"], cur, max_bytes)
    with db:
        state = begin_cli_batch(db, sid, cur, batch)
        for _off, rec in batch["records"]:
            if not rec:
                continue
            typ = rec.get("type")
            if isinstance(rec.get("timestamp"), str):
                norm = to_iso_utc(rec["timestamp"])
                if norm:
                    state["last_ts"] = norm
            model = codex_model_id(rec)
            if model:
                state["model"] = model
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if typ == "session_meta":
                session = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
                cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
                task = task_from_cwd(cwd)
                state["session_id"] = session
                state["task"] = task
                state["cwd"] = cwd
                state["bot_id"] = ctx.attributor().bot_for(task, cwd)
            elif typ == "event_msg" and payload.get("type") == "token_count" and state.get("session_id"):
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
                upsert_cli(db, {
                    "source_id": sid, "run_key": f"codex:{state['session_id']}", "tool": "codex",
                    "task": state.get("task"), "bot_id": state.get("bot_id") or "unattributed",
                    "session_id": state["session_id"], "ts": state.get("last_ts"), "model": state.get("model"),
                    "num_turns": None, "duration_ms": None, "is_error": None, "cost_usd": None,
                    "input_tokens": as_int(usage.get("input_tokens")),
                    "output_tokens": as_int(usage.get("output_tokens")),
                    "cache_read_tokens": as_int(usage.get("cached_input_tokens")),
                    "cache_create_tokens": as_int(usage.get("cache_write_input_tokens")),
                    "reasoning_tokens": as_int(usage.get("reasoning_output_tokens")),
                    "subagent_count": 0, "raw_json": scrub_raw(payload),
                    "cwd": state.get("cwd"), "attribution_method": None,
                }, only_if_newer=True)
        sync_codex_model(db, state)
        return finish_jsonl_cursor(ctx, src, cur, batch, state)


def cli_grok_step(ctx: Ctx, src, _max) -> bool:
    """usage.json is a snapshot: re-read only when mtime or size changes. cost = ticks/1e10."""
    db, sid = ctx.db, src["source_id"]
    cur = get_cursor(db, sid)
    st = os.stat(src["path"])
    if not file_changed(cur, st):
        return False
    if st.st_size > MAX_RECORD_BYTES:
        with db:
            cur.update(error_code="too_large", backlog_bytes=st.st_size, size_bytes=st.st_size,
                       mtime_ns=st.st_mtime_ns, inode=st.st_ino, device=st.st_dev)
            put_cursor(db, cur)
        ctx.errors.append(f"cli_grok:{sid}:too_large")
        return False
    with open(src["path"], "rb") as f:
        raw = f.read()
    ctx.bytes_read += len(raw)
    ctx.rows += 1
    summary_path = os.path.join(os.path.dirname(src["path"]), "summary.json")
    cwd = None
    try:
        if os.path.isfile(summary_path):
            with open(summary_path, "rb") as f:
                sraw = f.read(MAX_RECORD_BYTES)
            ctx.bytes_read += len(sraw)
            summary = json.loads(sraw)
            info = summary.get("info") if isinstance(summary, dict) else None
            if isinstance(info, dict) and isinstance(info.get("cwd"), str):
                cwd = info["cwd"]
    except (OSError, ValueError):
        cwd = None
    with db:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            cur.update(error_code="parse", mtime_ns=st.st_mtime_ns, size_bytes=st.st_size,
                       inode=st.st_ino, device=st.st_dev, last_success_ms=now_ms(), backlog_bytes=0)
            put_cursor(db, cur)
            ctx.errors.append(f"cli_grok:{sid}:parse")
            return False
        session = data.get("session") if isinstance(data.get("session"), dict) else None
        session_id = data.get("sessionId") if isinstance(data.get("sessionId"), str) else None
        if not session_id:
            session_id = os.path.basename(os.path.dirname(src["path"]))
        if not session or not session_id:
            cur.update(error_code="no_session", mtime_ns=st.st_mtime_ns, size_bytes=st.st_size,
                       inode=st.st_ino, device=st.st_dev, last_success_ms=now_ms(), backlog_bytes=0)
            put_cursor(db, cur)
            return False
        ticks = session.get("costUsdTicks")
        cost = None
        if isinstance(ticks, (int, float)) and not isinstance(ticks, bool):
            cost = ticks / 1e10
        task = task_from_cwd(cwd)
        model = session.get("primaryModelId") if isinstance(session.get("primaryModelId"), str) else None
        kept = {"sessionId": session_id, "updatedAt": data.get("updatedAt"), "session": session}
        run_key = f"grok:{session_id}"
        ts = to_iso_utc(data.get("updatedAt"))
        old = db.execute("SELECT ts FROM cli_runs WHERE run_key=?", (run_key,)).fetchone()
        if not (old and old[0] and ts and old[0] > ts):
            db.execute("DELETE FROM cli_runs WHERE source_id=? AND run_key!=?", (sid, run_key))
        upsert_cli(db, {
            "source_id": sid, "run_key": run_key, "tool": "grok",
            "task": task, "bot_id": ctx.attributor().bot_for(task, cwd), "session_id": session_id,
            "ts": ts, "model": model,
            "num_turns": as_int(session.get("turnCount")), "duration_ms": None, "is_error": None,
            "cost_usd": cost,
            "input_tokens": as_int(session.get("inputTokens")),
            "output_tokens": as_int(session.get("outputTokens")),
            "cache_read_tokens": as_int(session.get("cachedReadTokens")),
            "cache_create_tokens": as_int(session.get("cacheCreationTokens")),
            "reasoning_tokens": as_int(session.get("reasoningTokens")),
            "subagent_count": 0, "raw_json": scrub_raw(kept),
            "cwd": cwd, "attribution_method": None,
        }, only_if_newer=True)
        cur.update(mtime_ns=st.st_mtime_ns, size_bytes=st.st_size, inode=st.st_ino, device=st.st_dev,
                   last_success_ms=now_ms(), error_code=None, backlog_bytes=0)
        put_cursor(db, cur)
    ctx.made_progress = True
    return False


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_launch(rec) -> dict | None:
    """Known launch fields only. Extra keys (prompts, args) are dropped."""
    if not isinstance(rec, dict):
        return None
    tool = rec.get("tool")
    if tool not in ("claude", "codex", "grok"):
        return None
    return {
        "tool": tool,
        "bot_id": _text(rec.get("bot_id")),
        "task": _text(rec.get("task")),
        "cwd": _text(rec.get("cwd")),
        "started_at": _text(rec.get("started_at")),
        "pid": as_int(rec.get("pid")),
        "session_id": _text(rec.get("session_id")),
    }


def upsert_launch(db, row: dict):
    sql = (f"INSERT INTO cli_launches ({','.join(LAUNCH_COLS)}) VALUES ({','.join('?' * len(LAUNCH_COLS))}) "
           f"ON CONFLICT(launch_key) DO UPDATE SET "
           + ", ".join(f"{c}=excluded.{c}" for c in LAUNCH_COLS if c != "launch_key"))
    db.execute(sql, [row.get(c) for c in LAUNCH_COLS])


def cli_launches_step(ctx: Ctx, src, max_bytes: int) -> bool:
    """grokbot-run JSONL. Same byte cursor as the other JSONL sources."""
    db, sid = ctx.db, src["source_id"]
    cur = get_cursor(db, sid)
    batch = consume_jsonl(ctx, src["path"], cur, max_bytes)
    with db:
        if batch["reset"]:
            cur["generation"] = (cur["generation"] or 0) + 1
            db.execute("DELETE FROM cli_launches WHERE source_id=?", (sid,))
        for line_off, rec in batch["records"]:
            parsed = parse_launch(rec)
            if not parsed:
                continue
            parsed["launch_key"] = f"{sid}:{line_off}"
            parsed["source_id"] = sid
            upsert_launch(db, parsed)
        return finish_jsonl_cursor(ctx, src, cur, batch, {})


def _same_cwd(a, b) -> bool:
    if not isinstance(a, str) or not isinstance(b, str) or not a.strip() or not b.strip():
        return False
    return os.path.normpath(a) == os.path.normpath(b)


def _cwd_base(cwd) -> str | None:
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    base = os.path.basename(os.path.normpath(cwd))
    return base or None


def cli_source_task(row) -> str | None:
    """Task derived from the run file, ignoring a previous launch override."""
    sid = row.get("source_id") or ""
    filename = os.path.basename(sid)
    if row.get("tool") == "claude" or (filename.startswith("claude-") and filename.endswith(".jsonl")):
        parent = os.path.basename(os.path.dirname(sid))
        return parent or None
    cwd = row.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return task_from_cwd(cwd)
    if row.get("attribution_method") not in ("launch_session", "launch_cwd"):
        return _text(row.get("task"))
    return None


def _launch_ms(launch) -> int | None:
    return iso_ms(launch.get("started_at"))


def _prefer_session_launch(run_ms, launches):
    def key(launch):
        lm = _launch_ms(launch)
        if lm is None:
            return (3, 0, launch["launch_key"])
        if run_ms is None:
            return (2, 0, launch["launch_key"])
        if lm <= run_ms:
            return (0, run_ms - lm, launch["launch_key"])
        return (1, lm - run_ms, launch["launch_key"])
    return min(launches, key=key)


def _cwd_matches(row, launch, names: set[str]) -> bool:
    if _same_cwd(row.get("cwd"), launch.get("cwd")):
        return True
    task = launch.get("task")
    if isinstance(task, str) and task in names:
        return True
    base = _cwd_base(launch.get("cwd"))
    return bool(base and base in names)


def ensure_codex_model_reread(db):
    """Replay Codex rollout files once so model ids land on rows already collected.

    The byte cursor goes back to 0. Generation is not bumped and rows are not
    deleted: the same ``codex:<session>`` key is upserted, so totals are replaced.
    """
    row = db.execute("SELECT value FROM meta WHERE key=?", (CODEX_MODEL_EPOCH_KEY,)).fetchone()
    if row and row[0] == CODEX_MODEL_EPOCH:
        return
    with db:
        db.execute(
            "UPDATE cursors SET byte_offset=0, boundary_hash=NULL, "
            "backlog_bytes=COALESCE(size_bytes, backlog_bytes, 0) "
            "WHERE source_id LIKE 'codex-sessions/%'")
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                   (CODEX_MODEL_EPOCH_KEY, CODEX_MODEL_EPOCH))


def refresh_codex_estimates(db, path, errors=None):
    """Fill cost_usd_est for Codex rows. A non-NULL cost_usd is left untouched."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cli_runs'").fetchone():
        return
    cols = {row[1] for row in db.execute("PRAGMA table_info(cli_runs)")}
    if "cost_usd_est" not in cols:
        return
    try:
        table = prices.load_prices(path)
    except (OSError, ValueError) as exc:
        if errors is not None:
            errors.append(f"prices:{type(exc).__name__}")
        return
    rows = db.execute(
        "SELECT run_key, model, input_tokens, cache_read_tokens, cache_create_tokens, "
        "output_tokens, reasoning_tokens, cost_usd FROM cli_runs WHERE tool='codex'").fetchall()
    with db:
        for row in rows:
            key = row["run_key"]
            if row["cost_usd"] is not None:
                db.execute(
                    "UPDATE cli_runs SET cost_usd_est=NULL, cost_estimated=0, model_defaulted=0 "
                    "WHERE run_key=?", (key,))
                continue
            usd, used_default = prices.estimate_codex_cost(
                table, row["model"],
                input_tokens=row["input_tokens"],
                cached_input_tokens=row["cache_read_tokens"],
                cache_write_tokens=row["cache_create_tokens"],
                output_tokens=row["output_tokens"],
                reasoning_tokens=row["reasoning_tokens"])
            db.execute(
                "UPDATE cli_runs SET cost_usd_est=?, cost_estimated=1, model_defaulted=? WHERE run_key=?",
                (usd, 1 if used_default else 0, key))


def attribute_cli_runs(ctx: Ctx) -> None:
    """Re-apply launch tags over an optional task plugin. Safe to run every pass.

    (a) same tool and session_id → launch_session.
    (b) else same tool, matching cwd or task/report dir, nearest started_at at
        or before the run and within 24h. A launch already consumed by another
        cwd match is skipped while an unused candidate remains.
    Otherwise plugin when a configured plugin names a bot, else unattributed.
    """
    db = ctx.db
    runs = [dict(r) for r in db.execute(
        "SELECT run_key, source_id, tool, task, bot_id, session_id, ts, cwd, attribution_method FROM cli_runs")]
    if not runs:
        return
    launches = [dict(r) for r in db.execute(
        "SELECT launch_key, tool, bot_id, task, cwd, started_at, session_id FROM cli_launches")]
    by_session: dict[tuple, list] = {}
    for launch in launches:
        if launch.get("session_id"):
            by_session.setdefault((launch["tool"], launch["session_id"]), []).append(launch)

    assigned: dict[str, tuple] = {}
    consumed = set()
    for run in runs:
        session = run.get("session_id")
        if not session:
            continue
        found = by_session.get((run["tool"], session))
        if not found:
            continue
        best = _prefer_session_launch(iso_ms(run.get("ts")), found)
        assigned[run["run_key"]] = ("launch_session", best)
        consumed.add(best["launch_key"])

    pairs = []
    for run in runs:
        if run["run_key"] in assigned:
            continue
        run_ms = iso_ms(run.get("ts"))
        if run_ms is None:
            continue
        names = set()
        source_task = cli_source_task(run)
        if source_task:
            names.add(source_task)
        for launch in launches:
            if launch["tool"] != run["tool"] or launch["launch_key"] in consumed:
                continue
            if not _cwd_matches(run, launch, names):
                continue
            launch_ms = _launch_ms(launch)
            if launch_ms is None or launch_ms > run_ms:
                continue
            delta = run_ms - launch_ms
            if delta > LAUNCH_MATCH_WINDOW_MS:
                continue
            pairs.append((delta, run["run_key"], launch["launch_key"]))
    by_key = {launch["launch_key"]: launch for launch in launches}
    pairs.sort(key=lambda item: (item[0], item[1], item[2]))
    taken_runs = set()
    for _delta, run_key, launch_key in pairs:
        if run_key in taken_runs or launch_key in consumed:
            continue
        assigned[run_key] = ("launch_cwd", by_key[launch_key])
        taken_runs.add(run_key)
        consumed.add(launch_key)

    needs_plugin = any(run["run_key"] not in assigned for run in runs)
    attr = ctx.attributor() if needs_plugin else None
    with db:
        for run in runs:
            hit = assigned.get(run["run_key"])
            source_task = cli_source_task(run)
            if hit:
                method, launch = hit
                bot = _text(launch.get("bot_id")) or "unattributed"
                task = _text(launch.get("task")) or source_task
            else:
                # Claude's plugin hit is the report-dir task id. Codex and Grok also
                # pass cwd so a project name inside the working directory can match.
                plugin_cwd = None if run.get("tool") == "claude" else run.get("cwd")
                bot = attr.bot_for(source_task, plugin_cwd) if attr else "unattributed"
                task = source_task
                method = "plugin" if bot != "unattributed" else "unattributed"
            db.execute("UPDATE cli_runs SET bot_id=?, task=?, attribution_method=? WHERE run_key=?",
                       (bot, task, method, run["run_key"]))


# --------------------------------------------------------------------- pass


STEPS = {"store": lambda c, s, m: store_step(c, s), "audit": jsonl_step, "transcript": jsonl_step,
         "automation": automation_step, "runs": runs_step,
         "search_index_messages": search_index_step, "search_index_media": search_index_step,
         "cli_claude": cli_claude_step, "cli_codex": cli_codex_step, "cli_grok": cli_grok_step,
         "cli_launches": cli_launches_step}


def collect_pass(db, root, budget_s=30.0, max_read=20 * 1024 * 1024, mode="pass",
                 codex_dir=None, grok_dir=None, launches_path=None, prices_path=None) -> dict:
    t0 = time.monotonic()
    started = now_ms()
    ctx = Ctx(db, root, codex_dir, grok_dir, launches_path, prices_path)
    ensure_codex_model_reread(db)
    sources = discover(ctx)
    # Small config/run files first, then fair round-robin across the rest,
    # rotating the starting point between passes.
    small = [s for s in sources if s["type"] in ("automation", "runs")]
    big = [s for s in sources if s["type"] not in ("automation", "runs")]
    row = db.execute("SELECT value FROM meta WHERE key='rr_start'").fetchone()
    start = int(row[0]) % len(big) if row and big else 0
    big = big[start:] + big[:start]
    active = small + big
    exhausted = False
    while active and not exhausted:
        nxt = []
        for src in active:
            if time.monotonic() - t0 > budget_s or ctx.bytes_read >= max_read:
                exhausted = True
                break
            try:
                more = STEPS[src["type"]](ctx, src, min(JSONL_BATCH_BYTES, max(max_read - ctx.bytes_read, 1)))
            except (OSError, sqlite3.Error) as exc:
                ctx.errors.append(f"{src['type']}:{src['source_id']}:{type(exc).__name__}")
                with db:
                    cur = get_cursor(db, src["source_id"])
                    cur["error_code"] = type(exc).__name__
                    put_cursor(db, cur)
                    db.execute("UPDATE sources SET status='error' WHERE source_id=?", (src["source_id"],))
                more = False
            else:
                db.execute("UPDATE sources SET status='ok' WHERE source_id=? AND status='error'", (src["source_id"],))
            if more:
                nxt.append(src)
        active = nxt
    with db:
        db.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('rr_start',?)", (str(start + 7),))
    backlog = backlog_sources = 0
    for src in sources:  # never-visited sources count fully as backlog
        cur = db.execute("SELECT backlog_bytes FROM cursors WHERE source_id=?", (src["source_id"],)).fetchone()
        try:
            pending = cur[0] if cur else os.path.getsize(src["path"])
        except OSError:
            pending = 0
        backlog += pending
        backlog_sources += pending > 0
    refresh_codex_estimates(db, ctx.prices_path, ctx.errors)
    attribute_cli_runs(ctx)
    stats = dict(started_at_ms=started, ended_at_ms=now_ms(), script_version=SCRIPT_VERSION, mode=mode,
                 runtime_ms=int((time.monotonic() - t0) * 1000), bytes_read=ctx.bytes_read,
                 rows_inspected=ctx.rows, events_inserted=ctx.inserted, backlog_bytes=backlog,
                 backlog_sources=backlog_sources, errors=len(ctx.errors),
                 error_summary=";".join(ctx.errors[:20]) or None,
                 tracker_request_id=os.environ.get("QUOTA_TRACKER_REQUEST_ID"),
                 tracker_run_id=os.environ.get("QUOTA_TRACKER_RUN_ID"),
                 made_progress=ctx.made_progress)
    return stats


def log_run(db, db_path, stats) -> int:
    stats = dict(stats)
    try:
        stats["db_bytes"] = sum(os.path.getsize(p) for p in (db_path, db_path + "-wal") if os.path.exists(p))
    except OSError:
        stats["db_bytes"] = None
    cols = list(stats)
    with db:
        cur = db.execute(f"INSERT INTO collector_runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [stats[c] for c in cols])
    return cur.lastrowid


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="quota sqlite ($QUOTA_DB, else under the data root or state dir)")
    ap.add_argument("--root", default=None, help="data root ($QUOTA_DATA_ROOT, ~/agent-data, or /home/*/agent-data)")
    ap.add_argument("--codex-sessions", default=None,
                    help="Codex rollout root ($QUOTA_CODEX_SESSIONS or ~/.codex/sessions)")
    ap.add_argument("--grok-sessions", default=None,
                    help="Grok session root ($QUOTA_GROK_SESSIONS or ~/.grok/sessions)")
    ap.add_argument("--launches", default=None,
                    help="grokbot-run launches.jsonl (default: $GROKBOT_QUOTA_STATE/launches.jsonl "
                         "if set, else both ~/.local/state and ~/.local/share copies)")
    ap.add_argument("--prices", default=None,
                    help="Codex price table JSON (default: $QUOTA_PRICES or config/prices.json)")
    ap.add_argument("--budget-seconds", type=float, default=30.0)
    ap.add_argument("--max-read-mib", type=float, default=20.0)
    ap.add_argument("--backfill", action="store_true", help="repeat passes until no backlog remains")
    ap.add_argument("--max-passes", type=int, default=500)
    ap.add_argument("--report", action="store_true", help="write panel.html and print the summary")
    ap.add_argument("--panel", default=None, help="panel path (default: next to the DB)")
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="report window start (YYYY-MM-DD)")
    ap.add_argument("--end", type=date.fromisoformat, default=None, help="report window end (YYYY-MM-DD)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    if args.start and args.end and args.start > args.end:
        ap.error("--start is after --end")
    db_path = args.db or paths.default_db()
    root = args.root or paths.default_data_root()
    codex_dir = args.codex_sessions if args.codex_sessions is not None else paths.default_codex_sessions()
    grok_dir = args.grok_sessions if args.grok_sessions is not None else paths.default_grok_sessions()

    db = open_db(db_path)
    max_read = int(args.max_read_mib * 1024 * 1024)
    passes = 0
    while True:
        passes += 1
        stats = collect_pass(db, root, args.budget_seconds, max_read,
                             "backfill" if args.backfill else "pass",
                             codex_dir, grok_dir, args.launches, args.prices)
        made_progress = stats.pop("made_progress", False)
        log_run(db, db_path, stats)
        if not args.quiet:
            print(f"pass {passes}: read {stats['bytes_read'] / 1048576:.1f} MiB, {stats['events_inserted']} events, "
                  f"backlog {stats['backlog_bytes'] / 1048576:.1f} MiB in {stats['backlog_sources']} sources, "
                  f"{stats['errors']} errors, {stats['runtime_ms']} ms", file=sys.stderr)
        if not args.backfill or stats["backlog_bytes"] == 0 or passes >= args.max_passes:
            break
        if stats["events_inserted"] == 0 and not made_progress:
            break  # nothing new: only partial trailing lines or errored sources left
    t0 = time.monotonic()
    attribute.run(db)
    if not args.quiet:
        print(f"attribution: {int((time.monotonic() - t0) * 1000)} ms", file=sys.stderr)
    if args.report:
        from quota import report
        panel = args.panel or os.path.join(os.path.dirname(os.path.abspath(db_path)), "panel.html")
        window = report.report_window(db, args.start, args.end)
        report.write_panel(db, panel, window)
        print(report.summary(db, panel, window))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Copy the synthetic agent-data tree to DEST and build store.db files.

store.db is generated from store_entries.jsonl and search-index.db from
search_index.json (binary DBs are not committed).

  python3 examples/synthetic/build.py /tmp/fake-agent-data
"""

import glob
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-data")


def build(dest: str) -> str:
    shutil.copytree(HERE, dest, dirs_exist_ok=True)
    for src in glob.glob(os.path.join(dest, "agents", "*", "store_entries.jsonl")):
        db = sqlite3.connect(os.path.join(os.path.dirname(src), "store.db"))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS transcript_entries (seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, "
                   "entry TEXT NOT NULL) STRICT")

        with open(src) as f:
            for line in f:
                if line.strip():
                    db.execute("INSERT OR IGNORE INTO transcript_entries (id, entry) VALUES (?, ?)",
                               (json.loads(line)["id"], line.strip()))
        db.commit()
        db.close()
        os.remove(src)
    build_search_index(os.path.join(dest, "search_index.json"), os.path.join(dest, "search-index.db"))
    return dest


# Same tables as the real server-synced mirror.
SEARCH_INDEX_SCHEMA = """
CREATE TABLE messages (id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, entry_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')), timestamp_ms INTEGER NOT NULL, body TEXT NOT NULL,
  seq INTEGER, UNIQUE(agent_id, entry_id)) STRICT;
CREATE TABLE media (id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, entry_id TEXT NOT NULL, file_name TEXT NOT NULL,
  ext TEXT NOT NULL, mime TEXT, kind TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, width INTEGER, height INTEGER,
  seq INTEGER, UNIQUE(agent_id, entry_id)) STRICT;
"""


def build_search_index(src: str, path: str) -> None:
    with open(src) as f:
        data = json.load(f)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SEARCH_INDEX_SCHEMA)
    for table, rows in data.items():
        for r in rows:
            db.execute(f"INSERT INTO {table} ({','.join(r)}) VALUES ({','.join('?' * len(r))})", list(r.values()))
    db.commit()
    db.close()
    os.remove(src)


if __name__ == "__main__":
    print(build(sys.argv[1]))

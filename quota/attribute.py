"""Rebuildable attribution projections over the event ledger.

Fills, for every event:
  est_at_ms / est_day   time estimate and its Europe/Lisbon date
  root_entity_id        causal owner: top of the parent-child chain
  attr_run_id/_routine  routine run the event belongs to, with confidence

and the detail_spans table: per entity, the time span its detailed transcript
covers (search-index events inside it are not scored, see v_scored_activity).

Time estimate by time_basis:
  exact         source timestamp (store, audit, runs)
  window        transcript line appended after the file was first read:
                file mtime at read time (accurate to one collection interval)
  interpolated  backfilled transcript line. Bots: linear in byte position
                between the bot's first and last exact timestamp. Children:
                time of the parent event that links to them. Else undated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Lisbon")
CONF_RANK = {"high": 3, "medium": 2, "low": 1}


def lisbon_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d")


def parent_map(db) -> dict[str, tuple[str, int | None, str]]:
    """child -> (parent, evidence_event_id, confidence); best link wins."""
    best: dict[str, tuple] = {}
    for frm, to, ev, conf in db.execute(
            "SELECT from_id, to_id, evidence_event_id, confidence FROM links WHERE relation='parent-child'"):
        if frm.startswith("sand-subagent-") and not to.startswith("sand-subagent-"):
            continue  # a child never parents a bot
        key = (CONF_RANK.get(conf, 0), -(ev or 10**12))
        if to not in best or key > best[to][0]:
            best[to] = (key, frm, ev, conf)
    return {child: (v[1], v[2], v[3]) for child, v in best.items()}


def roots(parents: dict) -> dict[str, tuple[str, str]]:
    """entity -> (root, weakest confidence on the path)."""
    out = {}
    for child in parents:
        node, conf, seen = child, "high", {child}
        while node in parents:
            parent, _, c = parents[node]
            if parent in seen:
                break  # cycle guard
            seen.add(parent)
            if CONF_RANK[c] < CONF_RANK[conf]:
                conf = c
            node = parent
        out[child] = (node, conf)
    return out


def run(db) -> None:
    with db:
        db.execute("INSERT OR IGNORE INTO entities (entity_id, kind, profile_present) "
                   "SELECT DISTINCT entity_id, CASE WHEN entity_id LIKE 'sand-subagent-%' THEN 'child' "
                   "ELSE 'unknown' END, 0 FROM events WHERE entity_id IS NOT NULL")
        db.execute("UPDATE events SET est_at_ms = CASE time_basis WHEN 'exact' THEN occurred_at_ms "
                   "WHEN 'window' THEN win_hi_ms ELSE NULL END")
        # Bots: interpolate backfilled transcript lines between first/last exact timestamps.
        # The search-index mirror outlives the transcripts, so it never widens the bounds.
        db.execute("DROP TABLE IF EXISTS temp.bounds")
        db.execute("CREATE TEMP TABLE bounds AS SELECT entity_id, MIN(occurred_at_ms) lo, MAX(occurred_at_ms) hi "
                   "FROM events WHERE occurred_at_ms IS NOT NULL AND event_type != 'configuration_changed' "
                   "AND source_id NOT LIKE 'search-index.db#%' GROUP BY entity_id")
        db.execute("UPDATE events SET est_at_ms = (SELECT CAST(b.lo + events.win_frac * (b.hi - b.lo) AS INTEGER) "
                   "FROM temp.bounds b WHERE b.entity_id = events.entity_id) "
                   "WHERE time_basis = 'interpolated' AND entity_id NOT LIKE 'sand-subagent-%'")

        # Children: time of the linking parent event; repeat for nested chains.
        parents = parent_map(db)
        db.execute("DROP TABLE IF EXISTS temp.cp")
        db.execute("CREATE TEMP TABLE cp (child TEXT PRIMARY KEY, parent TEXT, evidence INTEGER, root TEXT, conf TEXT)")
        rmap = roots(parents)
        db.executemany("INSERT INTO temp.cp VALUES (?,?,?,?,?)",
                       [(c, p, ev, rmap[c][0], rmap[c][1]) for c, (p, ev, _) in parents.items()])
        for _ in range(6):
            cur = db.execute(
                "UPDATE events SET est_at_ms = (SELECT p.est_at_ms FROM temp.cp JOIN events p "
                "ON p.event_id = cp.evidence WHERE cp.child = events.entity_id) "
                "WHERE time_basis = 'interpolated' AND est_at_ms IS NULL AND entity_id IN (SELECT child FROM temp.cp)")
            if cur.rowcount == 0:
                break
        # A child linked only by id mention has no evidence event: use the
        # parent's earliest dated activity as a coarse fallback.
        db.execute("UPDATE events SET est_at_ms = (SELECT MIN(p.est_at_ms) FROM temp.cp JOIN events p "
                   "ON p.entity_id = cp.parent WHERE cp.child = events.entity_id) "
                   "WHERE time_basis = 'interpolated' AND est_at_ms IS NULL AND entity_id IN (SELECT child FROM temp.cp)")

        # Lisbon dates (DST-aware), cached per hour.
        cache: dict[int, str] = {}
        updates = []
        for eid, ms in db.execute("SELECT event_id, est_at_ms FROM events"):
            if ms is None:
                updates.append((None, eid))
                continue
            hour = ms // 3_600_000
            if hour not in cache:
                cache[hour] = lisbon_day(hour * 3_600_000)
            updates.append((cache[hour], eid))
        db.executemany("UPDATE events SET est_day = ? WHERE event_id = ?", updates)

        # Detailed transcript coverage per entity (current generations only): the
        # dated transcript lines plus, for interpolated files, the whole
        # interpolation range the file is spread over.
        db.execute("DELETE FROM detail_spans")
        db.execute("INSERT INTO detail_spans (entity_id, lo_ms, hi_ms) SELECT entity_id, MIN(lo), MAX(hi) FROM ("
                   " SELECT e.entity_id, MIN(e.est_at_ms) lo, MAX(e.est_at_ms) hi FROM events e JOIN cursors c"
                   " ON c.source_id = e.source_id AND c.generation = e.generation"
                   " WHERE e.source_id LIKE 'agent-transcripts/%' AND e.est_at_ms IS NOT NULL GROUP BY e.entity_id"
                   " UNION ALL SELECT b.entity_id, b.lo, b.hi FROM temp.bounds b WHERE b.entity_id IN"
                   " (SELECT entity_id FROM events WHERE source_id LIKE 'agent-transcripts/%'"
                   " AND time_basis = 'interpolated')"
                   ") WHERE entity_id IS NOT NULL GROUP BY entity_id")

        # Causal owner.
        db.execute("UPDATE events SET root_entity_id = COALESCE("
                   "(SELECT root FROM temp.cp WHERE cp.child = events.entity_id), entity_id)")

        # Routine runs: exact requestId/run id first, then the run's time window.
        db.execute("UPDATE events SET attr_run_id = NULL, attr_routine_id = NULL, attr_confidence = NULL")
        db.execute("UPDATE events SET attr_run_id = (SELECT r.run_id FROM runs r WHERE r.run_id = events.run_id), "
                   "attr_confidence = 'high' WHERE run_id IS NOT NULL")
        db.execute("UPDATE events SET attr_run_id = (SELECT r.run_id FROM runs r WHERE r.request_id = events.request_id "
                   "LIMIT 1), attr_confidence = 'high' WHERE attr_run_id IS NULL AND request_id IN "
                   "(SELECT request_id FROM runs WHERE request_id IS NOT NULL)")
        db.execute("UPDATE events SET attr_run_id = (SELECT r.run_id FROM runs r WHERE r.owner_id = events.root_entity_id "
                   "AND events.est_at_ms BETWEEN r.started_at_ms AND r.finished_at_ms LIMIT 1), "
                   "attr_confidence = 'medium' WHERE attr_run_id IS NULL AND time_basis IN ('exact', 'window') "
                   "AND est_at_ms IS NOT NULL AND root_entity_id IN (SELECT owner_id FROM runs)")
        db.execute("UPDATE events SET attr_confidence = NULL WHERE attr_run_id IS NULL")
        db.execute("UPDATE events SET attr_routine_id = (SELECT r.routine_id FROM runs r "
                   "WHERE r.run_id = events.attr_run_id) WHERE attr_run_id IS NOT NULL")

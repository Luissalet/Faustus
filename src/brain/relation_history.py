"""Recorded-time history for the brain's existing valid-time relationships.

No second graph store. SQLite triggers capture changes in the same transaction.
Older state is not invented; the first legacy snapshot is dated at migration.
Physical forgetting deletes history too, so an as-of query cannot undo forget.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

_COLUMNS = ("id", "owner", "project", "src", "rel", "dst", "dst_value", "valid_from", "valid_until",
            "asserted_at", "evidence", "confidence", "method", "status", "superseded_by", "created_at", "updated_at")


def recorded_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _snapshot(alias):
    pairs = ", ".join(f"'{key}', {alias}.{key}" for key in _COLUMNS)
    return "json_object(" + pairs + ")"


def schema():
    return (
        "CREATE TABLE IF NOT EXISTS relation_versions (version INTEGER PRIMARY KEY AUTOINCREMENT, "
        "relation_id TEXT NOT NULL, owner TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_relation_versions_time ON relation_versions(owner, relation_id, recorded_at, version)",
        "CREATE INDEX IF NOT EXISTS idx_relation_versions_lookup ON relation_versions(relation_id, version)",
        "INSERT INTO relation_versions(relation_id, owner, recorded_at, payload) "
        f"SELECT r.id, r.owner, brain_relation_now(), {_snapshot('r')} FROM relations r "
        "WHERE NOT EXISTS (SELECT 1 FROM relation_versions v WHERE v.relation_id=r.id)",
        "CREATE TRIGGER IF NOT EXISTS relation_version_insert AFTER INSERT ON relations BEGIN "
        f"INSERT INTO relation_versions(relation_id, owner, recorded_at, payload) VALUES(NEW.id, NEW.owner, brain_relation_now(), {_snapshot('NEW')}); END",
        "CREATE TRIGGER IF NOT EXISTS relation_version_update AFTER UPDATE ON relations BEGIN "
        f"INSERT INTO relation_versions(relation_id, owner, recorded_at, payload) VALUES(NEW.id, NEW.owner, brain_relation_now(), {_snapshot('NEW')}); END",
        "CREATE TRIGGER IF NOT EXISTS relation_version_forget AFTER DELETE ON relations BEGIN "
        "DELETE FROM relation_versions WHERE relation_id=OLD.id; END",
    )


def at(owner, known_at, *, entity_id=None):
    from src.brain.db import db, parse_iso
    instant = parse_iso(known_at)
    if instant is None:
        raise ValueError("known_at must be an ISO date/time")
    stamp = instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with db() as conn:
        rows = conn.execute(
            "SELECT payload FROM relation_versions WHERE version IN ("
            "SELECT max(version) FROM relation_versions WHERE owner=? AND recorded_at<=? GROUP BY relation_id) "
            "ORDER BY version", (str(owner or ""), stamp)).fetchall()
    out = []
    for row in rows:
        data = json.loads(row["payload"])
        data["evidence"] = json.loads(data["evidence"] or "[]")
        if not entity_id or entity_id in (data["src"], data["dst"]):
            out.append(data)
    return out


def horizon(owner):
    from src.brain.db import db
    with db() as conn:
        row = conn.execute("SELECT min(recorded_at) AS since FROM relation_versions WHERE owner=?",
                           (str(owner or ""),)).fetchone()
    return row["since"]


def forget_reference(conn, owner, source_ref):
    """Remove forgotten citations from every snapshot within the cleanup transaction."""
    rows = conn.execute("SELECT version,payload FROM relation_versions WHERE owner=?", (owner,)).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        evidence = json.loads(payload["evidence"] or "[]")
        if source_ref in evidence:
            payload["evidence"] = json.dumps([ref for ref in evidence if ref != source_ref], ensure_ascii=False)
            conn.execute("UPDATE relation_versions SET payload=? WHERE version=?",
                         (json.dumps(payload, ensure_ascii=False), row["version"]))

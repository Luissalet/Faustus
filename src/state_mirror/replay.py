"""Verified materialization journal, independent of expiring probe history.

Record the actual committed transitions, including conflict reconciliation and
freshness timestamps. Re-running today's reducer on old probes is not equivalent.
Hashes detect accidental corruption; they are not signatures against a database
administrator. A bounded checkpoint preserves the full current state, not an
unbounded time-travel archive.
"""
import hashlib
import json

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS state_replay_heads (entity_id TEXT PRIMARY KEY, "
    "base TEXT NOT NULL, base_hash TEXT NOT NULL, head TEXT NOT NULL, head_hash TEXT NOT NULL, "
    "sequence INTEGER NOT NULL, baseline_sequence INTEGER NOT NULL, origin TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS state_replay_steps (entity_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
    "patch TEXT NOT NULL, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL, "
    "PRIMARY KEY(entity_id, sequence))",
)
KEEP_STEPS = 100


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def verify(conn, entity_id):
    try:
        return _verify(conn, entity_id)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("state journal is malformed") from exc


def _verify(conn, entity_id):
    row = conn.execute("SELECT * FROM state_replay_heads WHERE entity_id=?", [entity_id]).fetchone()
    if row is None:
        raise ValueError("no verified materialization journal exists for this entity")
    value = json.loads(row["base"])
    current_hash = digest(value)
    if current_hash != row["base_hash"]:
        raise ValueError("state checkpoint checksum mismatch")
    sequence = row["baseline_sequence"]
    steps = conn.execute("SELECT * FROM state_replay_steps WHERE entity_id=? ORDER BY sequence", [entity_id])
    count = 0
    for step in steps:
        if step["sequence"] != sequence + 1 or step["before_hash"] != current_hash:
            raise ValueError("state journal is incomplete or out of order")
        patch = json.loads(step["patch"])
        for key in patch["remove"]:
            value.pop(key, None)
        value.update(patch["set"])
        current_hash = digest(value)
        if current_hash != step["after_hash"]:
            raise ValueError("state transition checksum mismatch")
        sequence += 1
        count += 1
    if (sequence != row["sequence"] or current_hash != row["head_hash"]
            or digest(json.loads(row["head"])) != current_hash):
        raise ValueError("state journal head mismatch")
    return value, {"sha256": current_hash, "sequence": sequence,
                   "baseline_sequence": row["baseline_sequence"], "steps_verified": count,
                   "origin": row["origin"]}


def record(conn, previous, current):
    entity_id = current["entity_id"]
    row = conn.execute("SELECT * FROM state_replay_heads WHERE entity_id=?", [entity_id]).fetchone()
    before = previous or {}
    before_hash = digest(before)
    if row is None:
        conn.execute("INSERT INTO state_replay_heads VALUES (?,?,?,?,?,?,?,?)",
                     [entity_id, encode(before), before_hash, encode(before), before_hash, 0, 0,
                      "legacy_checkpoint" if previous else "empty_state"])
        sequence = 0
    else:
        if before_hash != row["head_hash"] or digest(json.loads(row["head"])) != row["head_hash"]:
            raise ValueError("materialized state differs from its journal; verify and rebuild before writing")
        sequence = row["sequence"]
    if current == before:
        return
    patch = {"set": {k: v for k, v in current.items() if k not in before or before[k] != v},
             "remove": sorted(set(before) - set(current))}
    after_hash = digest(current)
    conn.execute("INSERT INTO state_replay_steps VALUES (?,?,?,?,?)",
                 [entity_id, sequence + 1, encode(patch), before_hash, after_hash])
    conn.execute("UPDATE state_replay_heads SET head=?, head_hash=?, sequence=? WHERE entity_id=?",
                 [encode(current), after_hash, sequence + 1, entity_id])
    if (sequence + 1) % KEEP_STEPS == 0:
        # Verify BEFORE dropping any prefix, inside the same state-write transaction.
        value, receipt = verify(conn, entity_id)
        conn.execute("UPDATE state_replay_heads SET base=?, base_hash=?, baseline_sequence=? WHERE entity_id=?",
                     [encode(value), receipt["sha256"], receipt["sequence"], entity_id])
        conn.execute("DELETE FROM state_replay_steps WHERE entity_id=?", [entity_id])

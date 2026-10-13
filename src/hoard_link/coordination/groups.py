"""Shared durable group state; participant execution belongs to its native engine."""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from .spaces import DEFAULT_SPACE_ID, space_id

DEFAULT_GROUPS_DB = Path(os.environ.get("HOARD_COORDINATION_DATA_DIR", ".")) / "groups.db"


class GroupStore:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_GROUPS_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, definition TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                    driver TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    space_id TEXT NOT NULL DEFAULT 'ecosystem');
                CREATE TABLE IF NOT EXISTS group_turns (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    group_id TEXT NOT NULL, request_id TEXT NOT NULL, message TEXT NOT NULL,
                    definition TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(group_id, request_id));
                CREATE TABLE IF NOT EXISTS group_steps (
                    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, member_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, status TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(turn_id, member_id));
            """)
            if 'space_id' not in {r[1] for r in db.execute('PRAGMA table_info(groups)')}:
                db.execute("ALTER TABLE groups ADD COLUMN space_id TEXT NOT NULL DEFAULT 'ecosystem'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim_driver(self, group_id, identity):
        now = time.time()
        with self.lock, self.db() as db:
            return db.execute("UPDATE groups SET driver=?,lease_until=? WHERE id=? AND (driver='' OR driver=? OR lease_until<?)",
                              (identity, now + 180, group_id, identity, now)).rowcount == 1

    def release_driver(self, group_id, identity):
        with self.lock, self.db() as db:
            db.execute("UPDATE groups SET driver='',lease_until=0 WHERE id=? AND driver=?", (group_id, identity))

    def group(self, group_id, owner):
        with self.lock, self.db() as db:
            row = db.execute("SELECT * FROM groups WHERE id=? AND owner=?", (group_id, owner or "")).fetchone()
        if not row:
            raise KeyError("Group not found")
        return {**json.loads(row["definition"]), "archived": bool(row["archived"]), "revision": row["revision"]}

    def list(self, owner, space_id=None):
        with self.lock, self.db() as db:
            where, params = "owner=? AND archived=0", [owner or ""]
            if space_id:
                where += " AND space_id=?"
                params.append(space_id)
            return [json.loads(row["definition"]) for row in db.execute(
                "SELECT definition FROM groups WHERE " + where + " ORDER BY rowid DESC", params)]

    def save(self, definition, owner, expected_revision=None):
        group_id = definition["parentId"]
        sid = space_id(definition.get('space_id', DEFAULT_SPACE_ID))
        with self.lock, self.db() as db:
            existing = db.execute("SELECT owner,revision FROM groups WHERE id=?", (group_id,)).fetchone()
            if existing and existing["owner"] != (owner or ""):
                raise KeyError("Group not found")
            if expected_revision is not None and existing and existing['revision'] != expected_revision:
                from .council_store import RevisionConflict
                raise RevisionConflict(group_id, expected_revision, existing['revision'])
            db.execute("INSERT INTO groups(id,owner,definition,space_id) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "definition=excluded.definition,space_id=excluded.space_id,revision=groups.revision+1",
                       (group_id, owner or "", json.dumps(definition, ensure_ascii=False), sid))
        return self.group(group_id, owner)

    def enqueue(self, group_id, owner, message, request_id, mentions=()):
        definition = self.group(group_id, owner)
        if definition["archived"]:
            raise ValueError("Group is archived")
        if not message.strip():
            raise ValueError("Message is required")
        members = definition["participants"]
        if mentions:
            selected = [p for p in members if p["sessionId"] in mentions or p["personaName"] in mentions]
            if not selected:
                raise ValueError("Mentioned participant not found")
            members = selected
        turn_id = uuid.uuid4().hex
        with self.lock, self.db() as db:
            prior = db.execute("SELECT id FROM group_turns WHERE group_id=? AND request_id=?", (group_id, request_id)).fetchone()
            if prior:
                return prior["id"], False
            db.execute("INSERT INTO group_turns(id,group_id,request_id,message,definition,status,created) VALUES(?,?,?,?,?,'queued',?)",
                       (turn_id, group_id, request_id, message, json.dumps(definition, ensure_ascii=False), time.time()))
            for i, member in enumerate(members):
                db.execute("INSERT INTO group_steps(id,turn_id,member_id,ordinal,status) VALUES(?,?,?,?,'queued')",
                           (uuid.uuid4().hex, turn_id, member["sessionId"], i))
            db.execute("UPDATE groups SET revision=revision+1 WHERE id=?", (group_id,))
        return turn_id, True

    def turns(self, group_id):
        with self.lock, self.db() as db:
            rows = db.execute("SELECT * FROM group_turns WHERE group_id=? ORDER BY ordinal", (group_id,)).fetchall()
        return [{**dict(row), "definition": json.loads(row["definition"])} for row in rows]

    def steps(self, turn_id):
        with self.lock, self.db() as db:
            rows = db.execute("SELECT * FROM group_steps WHERE turn_id=? ORDER BY ordinal", (turn_id,)).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def start_step(self, step_id):
        """Acquire the current step, without reviving a removed or stopped member."""
        with self.lock, self.db() as db:
            row = db.execute('SELECT s.*,g.definition,t.status AS turn_status FROM group_steps s '
                'JOIN group_turns t ON t.id=s.turn_id JOIN groups g ON g.id=t.group_id WHERE s.id=?', (step_id,)).fetchone()
            if not row or row['status'] not in ('queued', 'running') or row['turn_status'] == 'stopped':
                return None
            members = json.loads(row['definition'])['participants']
            if row['status'] == 'queued' and row['member_id'] not in {m['sessionId'] for m in members}:
                detail = {**json.loads(row['detail']), 'member_removed': True}
                db.execute('UPDATE group_steps SET status="stopped",detail=? WHERE id=?', (json.dumps(detail), step_id))
                return None
            db.execute('UPDATE group_steps SET status="running" WHERE id=?', (step_id,))
            db.execute('UPDATE groups SET revision=revision+1 WHERE id=(SELECT group_id FROM group_turns WHERE id=?)', (row['turn_id'],))
            return {k: (json.loads(row[k]) if k == 'detail' else row[k]) for k in row.keys() if k not in ('definition', 'turn_status')}

    def update_step(self, step_id, expected_status=None, **patch):
        allowed = {"status", "text", "error", "detail"}
        if not patch or set(patch) - allowed:
            raise ValueError("Invalid step update")
        if "detail" in patch:
            patch["detail"] = json.dumps(patch["detail"], ensure_ascii=False)
        with self.lock, self.db() as db:
            changed = db.execute("UPDATE group_steps SET " + ",".join(key + "=?" for key in patch) + " WHERE id=?"
                + (' AND status=?' if expected_status is not None else ''),
                (*patch.values(), step_id, *((expected_status,) if expected_status is not None else ()))).rowcount
            if not changed:
                return
            db.execute("UPDATE groups SET revision=revision+1 WHERE id=(SELECT group_id FROM group_turns WHERE id=(SELECT turn_id FROM group_steps WHERE id=?))", (step_id,))

    def update_turn(self, turn_id, status):
        with self.lock, self.db() as db:
            db.execute("UPDATE group_turns SET status=? WHERE id=?", (status, turn_id))
            db.execute("UPDATE groups SET revision=revision+1 WHERE id=(SELECT group_id FROM group_turns WHERE id=?)", (turn_id,))

    def stop(self, group_id):
        with self.lock, self.db() as db:
            active = [dict(row) for row in db.execute(
                "SELECT s.* FROM group_steps s JOIN group_turns t ON t.id=s.turn_id WHERE t.group_id=? AND s.status='running'", (group_id,))]
            db.execute("UPDATE group_steps SET status='stopped' WHERE turn_id IN (SELECT id FROM group_turns WHERE group_id=?) AND status IN ('queued','running','waiting_user')", (group_id,))
            db.execute("UPDATE group_turns SET status='stopped' WHERE group_id=? AND status IN ('queued','running','waiting_user')", (group_id,))
            db.execute("UPDATE groups SET revision=revision+1 WHERE id=?", (group_id,))
        return [{**row, "detail": json.loads(row["detail"])} for row in active]

    def recoverable(self):
        with self.lock, self.db() as db:
            return [(r["id"], r["owner"]) for r in db.execute("SELECT DISTINCT g.id,g.owner FROM groups g JOIN group_turns t ON t.group_id=g.id WHERE t.status IN ('queued','running') AND g.archived=0")]

    def turn_status(self, turn_id):
        with self.db() as db:
            row = db.execute('SELECT status FROM group_turns WHERE id=?', (turn_id,)).fetchone()
        return row[0] if row else None

    def archive(self, group_id):
        with self.lock, self.db() as db:
            db.execute('UPDATE groups SET archived=1,revision=revision+1 WHERE id=?', (group_id,))


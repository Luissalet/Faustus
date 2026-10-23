"""Persistent intent for a conversation, separate from turn and scheduler state.

This stores the human's brief and explicit native references. Runs keep their
own authority for execution/results; a finished transport never completes a
conversation objective. SQLite transactions provide CAS across processes.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
import uuid


class RevisionConflict(ValueError):
    def __init__(self, revision):
        self.revision = revision
        super().__init__(f"Objective changed; current revision is {revision}")


class ObjectiveStore:
    def __init__(self, path=None, clock=time.time):
        if path is None:
            from src.constants import DATA_DIR
            path = Path(DATA_DIR) / "conversation_objectives.sqlite3"
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS objectives (
                    owner TEXT NOT NULL, session_id TEXT NOT NULL, id TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL, statement TEXT NOT NULL, context TEXT NOT NULL,
                    deliverable TEXT NOT NULL, refs TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    active_since REAL, active_seconds REAL NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '', continuation_id TEXT, continuation_revision INTEGER,
                    PRIMARY KEY(owner, session_id));
                CREATE TABLE IF NOT EXISTS objective_runs (
                    objective_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    linked_at REAL NOT NULL, client_message_id TEXT, PRIMARY KEY(objective_id, run_id));
                CREATE TABLE IF NOT EXISTS objective_versions (
                    objective_id TEXT NOT NULL, revision INTEGER NOT NULL, brief TEXT NOT NULL,
                    PRIMARY KEY(objective_id,revision));
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _public(self, db, row):
        if row is None:
            return None
        value = dict(row)
        value.pop("owner", None)
        value["refs"] = json.loads(value["refs"])
        value["active_seconds"] += max(0, self.clock() - value["active_since"]) if value["active_since"] is not None else 0
        value["runs"] = [dict(r) for r in db.execute(
            "SELECT run_id,revision,linked_at FROM objective_runs WHERE objective_id=? ORDER BY linked_at,run_id", (value["id"],))]
        for run in value["runs"]:
            version = db.execute("SELECT brief FROM objective_versions WHERE objective_id=? AND revision=?", (value["id"], run["revision"])).fetchone()
            run["brief"] = json.loads(version["brief"]) if version else None
        return value

    @staticmethod
    def _remember(db, row):
        brief = {key: row[key] for key in ("statement", "context", "deliverable", "status", "result")}
        brief["refs"] = json.loads(row["refs"])
        db.execute("INSERT OR IGNORE INTO objective_versions VALUES(?,?,?)", (row["id"], row["revision"], json.dumps(brief)))

    def get(self, owner, session_id):
        with self.connect() as db:
            return self._public(db, db.execute("SELECT * FROM objectives WHERE owner=? AND session_id=?", (owner, session_id)).fetchone())

    @staticmethod
    def validate(brief):
        if not isinstance(brief, dict) or set(brief) - {"statement", "context", "deliverable", "refs"}:
            raise ValueError("Invalid objective fields")
        result = {key: brief.get(key, "") for key in ("statement", "context", "deliverable")}
        if any(not isinstance(value, str) for value in result.values()) or not result["statement"].strip():
            raise ValueError("An objective statement is required")
        refs = brief.get("refs", [])
        if not isinstance(refs, list):
            raise ValueError("refs must be a list")
        unique = []
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) - {"kind", "id", "label", "workspace", "path", "scope", "project_id"}:
                raise ValueError("Invalid reference fields")
            if ref.get("kind") not in ("task", "agent", "resource", "schedule", "change"):
                raise ValueError("Unknown reference kind")
            if any(not isinstance(value, str) for value in ref.values()):
                raise ValueError("Reference values must be strings")
            if ref["kind"] in ("task", "agent", "schedule") and not ref.get("id"):
                raise ValueError("Native reference id is required")
            if ref["kind"] == "task" and not ref.get("project_id"):
                raise ValueError("Choose the task's existing project explicitly")
            if ref["kind"] in ("resource", "change") and (not ref.get("workspace") or not ref.get("path")):
                raise ValueError("Choose the workspace and file explicitly")
            if ref["kind"] == "change":
                if ref.get("scope") not in ("staged", "unstaged") or not ref.get("id"):
                    raise ValueError("Choose a change scope explicitly")
            if ref not in unique:
                unique.append(dict(ref))
        return {**result, "refs": unique}

    def save(self, owner, session_id, expected_revision, brief):
        if not session_id or type(expected_revision) is not int:
            raise ValueError("A saved conversation and integer revision are required")
        brief = self.validate(brief)
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM objectives WHERE owner=? AND session_id=?", (owner, session_id)).fetchone()
            revision = row["revision"] if row else 0
            if revision != expected_revision:
                raise RevisionConflict(revision)
            if row is None:
                db.execute("INSERT INTO objectives(owner,session_id,id,revision,statement,context,deliverable,refs,status,created_at,updated_at,active_since) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (owner, session_id, uuid.uuid4().hex, 1, brief["statement"], brief["context"], brief["deliverable"], json.dumps(brief["refs"]), "active", now, now, now))
            else:
                db.execute("UPDATE objectives SET revision=revision+1,statement=?,context=?,deliverable=?,refs=?,updated_at=? WHERE owner=? AND session_id=?",
                           (brief["statement"], brief["context"], brief["deliverable"], json.dumps(brief["refs"]), now, owner, session_id))
            updated = db.execute("SELECT * FROM objectives WHERE owner=? AND session_id=?", (owner, session_id)).fetchone()
            self._remember(db, updated)
            result = self._public(db, updated)
        return result

    def transition(self, owner, session_id, expected_revision, status, result=""):
        if status not in ("active", "paused", "complete") or not isinstance(result, str):
            raise ValueError("Invalid objective state or result")
        if status == "complete" and not result.strip():
            raise ValueError("Describe the delivered result before completing the objective")
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM objectives WHERE owner=? AND session_id=?", (owner, session_id)).fetchone()
            if row is None:
                raise KeyError(session_id)
            if type(expected_revision) is not int or row["revision"] != expected_revision:
                raise RevisionConflict(row["revision"])
            elapsed = row["active_seconds"] + (max(0, now - row["active_since"]) if row["active_since"] is not None else 0)
            db.execute("UPDATE objectives SET revision=revision+1,status=?,updated_at=?,active_since=?,active_seconds=?,result=?,continuation_id=? WHERE owner=? AND session_id=?",
                       (status, now, now if status == "active" else None, elapsed, result if status == "complete" else row["result"], row["continuation_id"], owner, session_id))
            updated = db.execute("SELECT * FROM objectives WHERE owner=? AND session_id=?", (owner, session_id)).fetchone()
            self._remember(db, updated)
            value = self._public(db, updated)
        return value

    def link_run(self, owner, session_id, run_id, client_message_id="", binding=None):
        if not run_id:
            raise ValueError("run_id is required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM objectives WHERE owner=? AND session_id=?", (owner, session_id)).fetchone()
            if row is None:
                return None
            captured = binding if binding is not None else row
            accepted_continuation = bool(client_message_id and row["continuation_id"] == client_message_id)
            if captured["id"] != row["id"] or (captured["status"] != "active" and not accepted_continuation):
                return None
            db.execute("INSERT OR IGNORE INTO objective_runs VALUES(?,?,?,?,?)", (row["id"], run_id, captured["revision"], self.clock(), client_message_id))
            return row["id"]

    def continuation_run(self, owner, session_id, client_message_id):
        with self.connect() as db:
            row = db.execute("SELECT r.run_id FROM objective_runs r JOIN objectives o ON o.id=r.objective_id WHERE o.owner=? AND o.session_id=? AND r.client_message_id=?", (owner, session_id, client_message_id)).fetchone()
            return row["run_id"] if row else None

    def delete_session(self, session_id):
        """Remove the brief with its deleted parent; native work and artifacts stay put."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [row[0] for row in db.execute("SELECT id FROM objectives WHERE session_id=?", (session_id,))]
            for identity in ids:
                db.execute("DELETE FROM objective_runs WHERE objective_id=?", (identity,))
                db.execute("DELETE FROM objective_versions WHERE objective_id=?", (identity,))
            db.execute("DELETE FROM objectives WHERE session_id=?", (session_id,))

    def reserve_continuation(self, owner, session_id, expected_revision):
        """Reserve one new intent. A retry of that revision returns the same outbox key."""
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM objectives WHERE owner=? AND session_id=?", (owner, session_id)).fetchone()
            if row is None:
                raise KeyError(session_id)
            if type(expected_revision) is not int:
                raise ValueError("Integer revision required")
            if row["continuation_revision"] == expected_revision:
                return {**self._public(db, row), "_continuation_fresh": False}
            if row["revision"] != expected_revision:
                raise RevisionConflict(row["revision"])
            token = "objective-" + uuid.uuid4().hex
            db.execute("UPDATE objectives SET status='active',revision=revision+1,continuation_id=?,continuation_revision=?,updated_at=?,active_since=COALESCE(active_since,?) WHERE owner=? AND session_id=?",
                       (token, expected_revision, now, now, owner, session_id))
            updated = db.execute("SELECT * FROM objectives WHERE owner=? AND session_id=?", (owner, session_id)).fetchone()
            self._remember(db, updated)
            return {**self._public(db, updated), "_continuation_fresh": True}


def get_store():
    return ObjectiveStore()


def context_message(owner, session_id):
    return _context_message(get_store().get(owner, session_id))


def _context_message(value):
    if value is None:
        return None
    # A saved user brief is user context, never a system instruction. Its state
    # is descriptive; it does not silently cancel new messages or schedules.
    brief = {key: value[key] for key in ("id", "revision", "status", "statement", "context", "deliverable", "refs")}
    return {"role": "user", "content": "[Saved conversation objective; user-provided context]\n" + json.dumps(brief, ensure_ascii=False)}


def inject_context(context, owner, session_id):
    value = get_store().get(owner, session_id)
    context.objective_binding = {key: value[key] for key in ("id", "revision", "status")} if value else None
    message = _context_message(value)
    if message is None:
        return
    messages = context.messages
    messages.insert(max(0, len(messages) - 1), message)
    routes = getattr(context, "route_messages", None)
    if isinstance(routes, list) and routes is not messages:
        routes.insert(max(0, len(routes) - 1), dict(message))

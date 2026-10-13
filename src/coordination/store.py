"""Atomic cursors and pending details, isolated by owner, binding and filter.

Poll is receipt, not acknowledgement. A cursor may advance only after its
details have been stored in the same transaction. Reading an excerpt cannot
mark a detail delivered; coverage must span the complete stored body.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .bindings import Binding, ProjectTemplate, CoordinationError, StaleBinding


def filter_key(thread_ids=()) -> str:
    thread_ids = tuple(thread_ids)
    if any(type(i) is not int or i < 1 for i in thread_ids):
        raise CoordinationError("thread ids must be positive integers")
    return json.dumps(sorted(set(thread_ids)), separators=(",", ":"))


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _inbox_binding(snapshot):
    # Keep the original ecosystem cache addressable; other spaces have their
    # own cursors, message details, delivery receipts and task observations.
    return snapshot.id if snapshot.space_id == 'ecosystem' else json.dumps(
        [snapshot.id, snapshot.space_id], separators=(',', ':'))


class Store:
    def __init__(self, root):
        self.path = Path(root) / "coordination.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bindings (
                    owner TEXT, id TEXT, body TEXT, PRIMARY KEY(owner,id));
                CREATE TABLE IF NOT EXISTS templates (
                    owner TEXT, project TEXT, body TEXT, PRIMARY KEY(owner,project));
                CREATE TABLE IF NOT EXISTS cursors (
                    owner TEXT, binding TEXT, filter TEXT, cursor INTEGER,
                    PRIMARY KEY(owner,binding,filter));
                CREATE TABLE IF NOT EXISTS details (
                    owner TEXT, binding TEXT, filter TEXT, message INTEGER,
                    body TEXT, digest TEXT, metadata TEXT, coverage TEXT,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner,binding,filter,message));
                CREATE TABLE IF NOT EXISTS deliveries (
                    owner TEXT, binding TEXT, filter TEXT, receipt TEXT,
                    message INTEGER, digest TEXT, start INTEGER, stop INTEGER,
                    PRIMARY KEY(owner,binding,filter,receipt));
                CREATE UNIQUE INDEX IF NOT EXISTS delivery_span ON deliveries
                    (owner,binding,filter,message,digest,start,stop);
                CREATE TABLE IF NOT EXISTS observations (
                    owner TEXT, binding TEXT, task INTEGER, receipt TEXT, body TEXT,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner,binding,task));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _get(db, owner, id):
        row = db.execute("SELECT body FROM bindings WHERE owner=? AND id=?", (owner, id)).fetchone()
        if not row:
            raise CoordinationError("coordination binding not found")
        return Binding(**json.loads(row["body"]))

    def get(self, owner, id):
        with self.connect() as db:
            return self._get(db, owner, id)

    def for_session(self, owner, session_id):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM bindings WHERE owner=?", (owner,)).fetchall()
        bindings = [Binding(**json.loads(row["body"])) for row in rows]
        active = [b for b in bindings if b.active and b.session_id == session_id]
        if len(active) != 1:
            raise CoordinationError("this session needs exactly one active coordination binding")
        return active[0]

    def session_bindings(self, owner, session_id):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM bindings WHERE owner=? ORDER BY rowid DESC", (owner,)).fetchall()
        return [b for row in rows if (b := Binding(**json.loads(row["body"]))).session_id == session_id]

    def save(self, binding: Binding, expected_revision=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM bindings WHERE owner=? AND id=?", (binding.owner, binding.id)).fetchone()
            previous = Binding(**json.loads(row["body"])) if row else None
            if expected_revision != (previous.revision if previous else None):
                raise StaleBinding("binding changed; reload before saving")
            binding = replace(binding, revision=previous.revision + 1 if previous else 1)
            # A server-owned identity cannot be moved between projects or sessions.
            if previous and any(getattr(previous, k) != getattr(binding, k)
                                for k in ("owner", "project_id", "session_id", "connector_id", "actor", "connector_fingerprint")):
                raise CoordinationError("create a new binding for another identity")
            if binding.active:
                others = db.execute("SELECT body FROM bindings WHERE NOT(owner=? AND id=?)",
                                    (binding.owner, binding.id)).fetchall()
                for row in others:
                    other = Binding(**json.loads(row["body"]))
                    if other.active and ((other.owner == binding.owner and other.session_id == binding.session_id) or
                            ((other.connector_id, other.actor) == (binding.connector_id, binding.actor) and
                             not (binding.template_id and other.owner == binding.owner and other.template_id == binding.template_id))):
                        raise CoordinationError("session and actor already have an active binding")
            db.execute("INSERT OR REPLACE INTO bindings VALUES (?,?,?)",
                       (binding.owner, binding.id, json.dumps(binding.serialise(), separators=(",", ":"))))
        return binding

    def _live(self, db, snapshot):
        live = self._get(db, snapshot.owner, snapshot.id)
        if not live.active or live != snapshot:
            raise StaleBinding("coordination binding was changed or revoked")
        if live.template_id:
            template = self._template(db, live.owner, live.project_id)
            if not template or not template.active or template.id != live.template_id or template.revision != live.template_revision:
                raise StaleBinding("project coordination changed or was revoked")
        return live

    @staticmethod
    def _template(db, owner, project_id):
        row = db.execute("SELECT body FROM templates WHERE owner=? AND project=?", (owner, project_id)).fetchone()
        return ProjectTemplate(**json.loads(row["body"])) if row else None

    def template(self, owner, project_id):
        with self.connect() as db:
            return self._template(db, owner, project_id)

    def save_template(self, template, *, expected_revision=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._template(db, template.owner, template.project_id)
            if expected_revision != (previous.revision if previous else None):
                raise StaleBinding("project coordination changed; reload before saving")
            if previous and (previous.id, previous.actor) != (template.id, template.actor):
                raise CoordinationError("project coordination identity cannot be replaced")
            if template.active:
                for row in db.execute("SELECT body FROM templates WHERE NOT(owner=? AND project=?)", (template.owner, template.project_id)):
                    other = ProjectTemplate(**json.loads(row["body"]))
                    if other.active and (other.connector_id, other.actor) == (template.connector_id, template.actor):
                        raise CoordinationError("coordination identity already belongs to another project")
                for row in db.execute("SELECT body FROM bindings"):
                    other = Binding(**json.loads(row["body"]))
                    if (other.active and (other.connector_id, other.actor) == (template.connector_id, template.actor)
                            and not (other.owner == template.owner and other.template_id == template.id)):
                        raise CoordinationError("coordination identity already belongs to another chat")
            template = replace(template, revision=previous.revision + 1 if previous else 1)
            db.execute("INSERT OR REPLACE INTO templates VALUES (?,?,?)",
                       (template.owner, template.project_id, json.dumps(template.serialise())))
        return template

    def materialize(self, template, session_id):
        """Reuse a project's configuration without sharing chat cursors or receipts.

        Template changes invalidate in-flight grants immediately. A subsequent
        run captures the new revision; explicit chat revocation stays revoked.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._template(db, template.owner, template.project_id)
            if current != template or not current.active:
                raise StaleBinding("project coordination changed or was revoked")
            rows = db.execute("SELECT body FROM bindings WHERE owner=? ORDER BY rowid DESC", (template.owner,)).fetchall()
            previous = next((b for row in rows if (b := Binding(**json.loads(row["body"]))).session_id == session_id), None)
            if previous and (not previous.active or not previous.template_id):
                return previous
            if previous and previous.template_id == template.id and previous.template_revision == template.revision:
                return previous
            same_connector = previous and previous.template_id == template.id and previous.connector_id == template.connector_id
            binding = template.for_session(session_id, identifier=previous.id if same_connector else None)
            if previous:
                # A new connector starts with its own inbox and observations.
                if not same_connector:
                    db.execute("UPDATE bindings SET body=? WHERE owner=? AND id=?",
                               (json.dumps(replace(previous, active=False).serialise()), previous.owner, previous.id))
                else:
                    binding = replace(binding, revision=previous.revision + 1)
                    if previous.space_id == binding.space_id:
                        for table in ("observations", "deliveries"):
                            db.execute(f"DELETE FROM {table} WHERE owner=? AND binding=?", (previous.owner, _inbox_binding(previous)))
            db.execute("INSERT OR REPLACE INTO bindings VALUES (?,?,?)",
                       (binding.owner, binding.id, json.dumps(binding.serialise())))
        return binding

    def live(self, snapshot):
        with self.connect() as db:
            return self._live(db, snapshot)

    def cursor(self, snapshot, threads=()):
        key = filter_key(threads)
        with self.connect() as db:
            self._live(db, snapshot)
            row = db.execute("SELECT cursor FROM cursors WHERE owner=? AND binding=? AND filter=?",
                             (snapshot.owner, _inbox_binding(snapshot), key)).fetchone()
        return row["cursor"] if row else 0

    def receive(self, snapshot, *, expected_cursor, next_cursor, messages, threads=(), pending_messages=()):
        if type(expected_cursor) is not int or type(next_cursor) is not int or not 0 <= expected_cursor <= next_cursor:
            raise CoordinationError("invalid poll cursor")
        threads = tuple(threads)
        key = filter_key(threads)
        messages = list(messages)
        pending_messages = list(pending_messages)
        if len(messages) > 500 or len(pending_messages) > 200:
            raise CoordinationError("poll page exceeds the message limit")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._live(db, snapshot)
            row = db.execute("SELECT cursor FROM cursors WHERE owner=? AND binding=? AND filter=?",
                             (snapshot.owner, _inbox_binding(snapshot), key)).fetchone()
            current = row["cursor"] if row else 0
            if current != expected_cursor:
                raise StaleBinding("poll cursor changed; do not overwrite a concurrent poll")
            for index, msg in enumerate(messages + pending_messages):
                if not isinstance(msg, dict):
                    raise CoordinationError("invalid message detail")
                mid, body = msg.get("id"), msg.get("body")
                if type(mid) is not int or mid < 1 or not isinstance(body, str):
                    raise CoordinationError("invalid message detail")
                if threads and msg.get("thread_id") not in threads:
                    raise CoordinationError("message is outside the poll filter")
                if index < len(messages) and mid > next_cursor:
                    raise CoordinationError("cursor does not cover the stored message")
                digest = _digest(body)
                existing = db.execute("SELECT digest FROM details WHERE owner=? AND binding=? AND filter=? AND message=?",
                                      (snapshot.owner, _inbox_binding(snapshot), key, mid)).fetchone()
                if existing and existing["digest"] == digest:
                    continue  # An inbox replay cannot reset delivered coverage.
                metadata = {k: msg.get(k) for k in ("id", "thread_id", "author", "kind", "created", "relevant")}
                db.execute("INSERT OR REPLACE INTO details VALUES (?,?,?,?,?,?,?,?,0)",
                           (snapshot.owner, _inbox_binding(snapshot), key, mid, body, digest,
                            json.dumps(metadata, ensure_ascii=False), "[]"))
            db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)",
                       (snapshot.owner, _inbox_binding(snapshot), key, next_cursor))

    def pending(self, snapshot, threads=(), limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CoordinationError("invalid detail limit")
        key = filter_key(threads)
        with self.connect() as db:
            self._live(db, snapshot)
            rows = db.execute("SELECT message,substr(body,1,240) AS excerpt,length(body) AS chars,digest,metadata FROM details WHERE owner=? AND binding=? AND filter=? AND delivered=0 AND json_extract(metadata,'$.relevant') IS NOT 0 ORDER BY message LIMIT ?",
                              (snapshot.owner, _inbox_binding(snapshot), key, limit)).fetchall()
        return [{**json.loads(r["metadata"]), "body_excerpt": r["excerpt"],
                 "body_chars": r["chars"], "digest": r["digest"], "detail_ref": f"message:{r['message']}"}
                for r in rows]

    def pending_count(self, snapshot, threads=()):
        key = filter_key(threads)
        with self.connect() as db:
            self._live(db, snapshot)
            return db.execute("SELECT count(*) FROM details WHERE owner=? AND binding=? AND filter=? AND delivered=0 AND json_extract(metadata,'$.relevant') IS NOT 0",
                              (snapshot.owner, _inbox_binding(snapshot), key)).fetchone()[0]

    def detail(self, snapshot, message_id, *, threads=(), offset=0, length=2000, expected_digest):
        if type(message_id) is not int or message_id < 1 or type(offset) is not int or offset < 0:
            raise CoordinationError("invalid detail position")
        if type(length) is not int or not 1 <= length <= 4000:
            raise CoordinationError("invalid detail length")
        key = filter_key(threads)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._live(db, snapshot)
            row = db.execute("SELECT * FROM details WHERE owner=? AND binding=? AND filter=? AND message=?",
                             (snapshot.owner, _inbox_binding(snapshot), key, message_id)).fetchone()
            if not row or row["digest"] != expected_digest:
                raise CoordinationError("message detail not found or changed")
            body = row["body"]
            if offset > len(body):
                raise CoordinationError("offset exceeds the message")
            end = min(offset + length, len(body))
            receipt = uuid.uuid4().hex
            db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?,?,?,?,?)",
                       (snapshot.owner, _inbox_binding(snapshot), key, receipt, message_id, expected_digest, offset, end))
            receipt = db.execute("SELECT receipt FROM deliveries WHERE owner=? AND binding=? AND filter=? AND message=? AND digest=? AND start=? AND stop=?",
                                 (snapshot.owner, _inbox_binding(snapshot), key, message_id, expected_digest, offset, end)).fetchone()["receipt"]
        # Fetching is not delivery: a dropped response must leave the detail pending.
        return {"message_id": message_id, "digest": expected_digest, "offset": offset,
                "body": body[offset:end], "next_offset": end if end < len(body) else None,
                "receipt": receipt, "hub_acknowledged": False}

    def confirm_delivery(self, snapshot, receipt, *, threads=()):
        """Confirm a returned chunk after it reaches the native tool context.

        The product's delivery callback supplies this receipt, never a poll
        summary. Unknown/stale receipts cannot acknowledge another message.
        """
        key = filter_key(threads)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._live(db, snapshot)
            delivery = db.execute("SELECT * FROM deliveries WHERE owner=? AND binding=? AND filter=? AND receipt=?",
                                  (snapshot.owner, _inbox_binding(snapshot), key, receipt)).fetchone()
            if not delivery:
                raise CoordinationError("delivery receipt not found")
            message_id = delivery["message"]
            row = db.execute("SELECT * FROM details WHERE owner=? AND binding=? AND filter=? AND message=?",
                             (snapshot.owner, _inbox_binding(snapshot), key, message_id)).fetchone()
            if not row or row["digest"] != delivery["digest"]:
                raise CoordinationError("delivered message changed")
            spans = sorted([*json.loads(row["coverage"]), [delivery["start"], delivery["stop"]]])
            merged = []
            for start, stop in spans:
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], stop)
                else:
                    merged.append([start, stop])
            complete = merged == [[0, len(row["body"])]]
            db.execute("UPDATE details SET coverage=?,delivered=? WHERE owner=? AND binding=? AND filter=? AND message=?",
                       (json.dumps(merged), int(complete), snapshot.owner, _inbox_binding(snapshot), key, message_id))
        return {"message_id": message_id, "complete": complete, "hub_acknowledged": False}

    def observe_task(self, snapshot, task, *, required_messages=()):
        if not isinstance(task, dict) or type(task.get("id")) is not int or task["id"] < 1:
            raise CoordinationError("invalid task observation")
        receipt = uuid.uuid4().hex
        task = {k: v for k, v in task.items() if not k.startswith("_coordination_")}
        task["_coordination_required_messages"] = list(required_messages) if required_messages is not None else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._live(db, snapshot)
            db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,0)",
                       (snapshot.owner, _inbox_binding(snapshot), task["id"], receipt, json.dumps(task)))
        return receipt

    def confirm_task_delivery(self, snapshot, receipt):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._live(db, snapshot)
            row = db.execute("SELECT task FROM observations WHERE owner=? AND binding=? AND receipt=?",
                             (snapshot.owner, _inbox_binding(snapshot), receipt)).fetchone()
            if not row:
                raise CoordinationError("task delivery receipt not found")
            db.execute("UPDATE observations SET delivered=1 WHERE owner=? AND binding=? AND receipt=?",
                       (snapshot.owner, _inbox_binding(snapshot), receipt))
        return row["task"]

    def observed_task(self, snapshot, task_id):
        with self.connect() as db:
            self._live(db, snapshot)
            row = db.execute("SELECT body FROM observations WHERE owner=? AND binding=? AND task=? AND delivered=1",
                             (snapshot.owner, _inbox_binding(snapshot), task_id)).fetchone()
        if not row:
            raise CoordinationError("read this task before acting on it")
        return json.loads(row["body"])

    def observed_task_for_thread(self, snapshot, thread_id):
        if type(thread_id) is not int or thread_id < 1:
            raise CoordinationError("thread_id must be a positive integer")
        with self.connect() as db:
            self._live(db, snapshot)
            rows = db.execute("SELECT body FROM observations WHERE owner=? AND binding=? AND delivered=1",
                              (snapshot.owner, _inbox_binding(snapshot))).fetchall()
        matches = [task for row in rows if (task := json.loads(row["body"])).get("thread_id") == thread_id]
        if len(matches) != 1:
            raise CoordinationError("read the task for this thread before posting, or use a configured coordination thread")
        return matches[0]

    def reviewable_task(self, snapshot, task_id):
        task = self.observed_task(snapshot, task_id)
        required = task.get("_coordination_required_messages")
        if not isinstance(required, list):
            raise CoordinationError("read this task and its delivery before reviewing")
        with self.connect() as db:
            self._live(db, snapshot)
            for message in required:
                row = db.execute("SELECT digest,delivered FROM details WHERE owner=? AND binding=? AND filter='[]' AND message=?",
                                 (snapshot.owner, _inbox_binding(snapshot), message["id"])).fetchone()
                if not row or row["digest"] != message["digest"] or not row["delivered"]:
                    raise CoordinationError("read the complete task discussion and submission before voting; excerpts are insufficient")
        return task

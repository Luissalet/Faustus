"""project_board.py — the project work board (issues), Lote 92 / OBJ-6.

Luis's own framing (BOARD_RESEARCH.md): a user should not have to know that
Faustus keeps two markdown files (`PENDIENTES.md`, `OBJETIVOS.md`) — "what's
pending on this project" belongs in a small, queryable store, not prose the
agent re-reads in full every turn. This module is that store: a per-project
issue tracker with a readable id (`FAU-12`: the project's board key plus a
per-project sequential number), a fixed, non-configurable vocabulary of type/
status/priority (Linear's lesson: a status CATEGORY drives logic, a status
NAME is just a label — see ``STATUS_CATEGORY`` below), simple graph links
(`blocks`/`blocked_by`/`relates_to`/`duplicate_of`/`discovered_from`, never a
forced hierarchy), and one pointed convenience an agent actually needs:
:func:`ready_issues` — "what can be worked on right now" without reasoning
over the whole backlog, Beads' ``bd ready`` translated into this codebase.

Self-contained SQLite (own file under DATA_DIR, own schema, own writer
transaction per call) — the same shape as ``src/question_store.py`` and
``src/chat_outbox.py``, not a table bolted onto ``core/database.py``. A
project's issues are entirely disposable relative to the rest of the app: a
corrupt or deleted ``board.sqlite3`` loses a task list, never a session, a
document or an account.

What this deliberately is NOT (BOARD_RESEARCH.md "Qué NO hacer"): no
configurable per-project workflow, no permissions-by-transition, no unlimited
custom fields, no query language. One vocabulary, shared by every project;
``STATUSES``/``TYPES``/``PRIORITIES`` are closed, not user-editable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

VERSION = 1
MAX_BODY_BYTES = 200_000

#: Closed vocabulary (BOARD_RESEARCH.md "Vocabulario fijo (no configurable)").
TYPES = ("bug", "idea", "feature", "task", "chore")
STATUSES = ("open", "in_progress", "blocked", "done", "wontfix", "duplicate")
PRIORITIES = ("P0", "P1", "P2", "P3")
LINK_KINDS = ("blocks", "blocked_by", "relates_to", "duplicate_of", "discovered_from")
REF_KINDS = ("commit", "session", "file", "url")
TERMINAL_STATUSES = ("done", "wontfix", "duplicate")
DEFAULT_TYPE = "task"
DEFAULT_PRIORITY = "P2"
DEFAULT_STATUS = "open"

#: Status -> category (Linear's own split: the category, not the label, is
#: what "ready"/"done" logic reasons about — see module docstring).
STATUS_CATEGORY: Mapping[str, str] = {
    "open": "unstarted",
    "in_progress": "started",
    "blocked": "started",
    "done": "completed",
    "wontfix": "cancelled",
    "duplicate": "cancelled",
}

_PRIORITY_ORDER = {p: i for i, p in enumerate(PRIORITIES)}

#: `blocks`/`blocked_by` and `relates_to` are stored in both directions (a
#: link from A to B also writes the mirror from B to A) so "what blocks me"
#: is a plain lookup on the ISSUE being asked about, never a scan of every
#: other issue's links. `duplicate_of`/`discovered_from` stay one-directional
#: — BOARD_RESEARCH.md's graph, not a forced hierarchy.
_MIRROR_KIND: Mapping[str, str] = {
    "blocks": "blocked_by",
    "blocked_by": "blocks",
    "relates_to": "relates_to",
}

_KEY_RE = re.compile(r"^[A-Z]{2,5}$")
_ISSUE_ID_RE = re.compile(r"\b([A-Za-z]{2,5}-\d+)\b")
#: Magic close words (English + Spanish, BOARD_RESEARCH.md "Conexión con
#: git" / CONTRATO_BOARD): a word from this set directly before an issue id
#: in a commit message closes it, the same "keyword + id in the text, no
#: webhook" pattern Linear/GitHub use.
_CLOSE_RE = re.compile(
    r"\b(?:fixe?s|fixed|fixing|close[sd]?|closing|resolve[sd]?|resolving|"
    r"complete[sd]?|completing|implement(?:s|ed|ing)?|"
    r"cierra|cerrando|cerrad[oa]|arregla(?:d[oa])?|arreglando|"
    r"resuelve|resuelto|resuelta|resolviendo)"
    r"\s*:?\s*([A-Za-z]{2,5}-\d+)",
    re.IGNORECASE,
)


class BoardError(ValueError):
    """Base error. ``error_class`` mirrors the vocabulary git_tools.py and
    docs/api/*.md already use — a route turns it into an HTTP body, a tool
    turns it into a result dict, both read the same attribute."""

    def __init__(self, message: str, error_class: str = "board.invalid"):
        super().__init__(message)
        self.error_class = error_class


class NotFoundError(BoardError):
    def __init__(self, message: str = "Issue not found"):
        super().__init__(message, "board.not_found")


class InvalidTransitionError(BoardError):
    def __init__(self, message: str):
        super().__init__(message, "board.invalid_transition")


class ClaimedError(BoardError):
    def __init__(self, message: str, holder: str = ""):
        super().__init__(message, "board.claimed")
        self.holder = holder


def default_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "board.sqlite3"


def _now() -> str:
    from src.contracts.base import now_iso
    return now_iso()


def _json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        raise BoardError("payload too large", "board.too_large")
    return text


def _labels(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for v in value:
        text = str(v or "").strip()
        if text and text not in out:
            out.append(text)
    return out[:40]


def key_valid(key: str) -> bool:
    return bool(_KEY_RE.match(str(key or "").strip()))


def derive_key(name: str) -> str:
    """A readable default board key from a project name — "LocalAI" -> "LOC",
    "Writer's Hoard" -> "WH", "Faustus" -> "FAU" (CONTRATO_BOARD's own
    examples). Multi-word names take initials; a single word takes its first
    three letters. Always editable afterwards via `PUT .../board/key`."""
    text = re.sub(r"[’']s\b", "", str(name or ""), flags=re.IGNORECASE)
    words = [w for w in re.split(r"[^A-Za-z0-9]+", text) if w]
    if len(words) >= 2:
        letters = [w[0].upper() for w in words if w[0].isalpha()]
        key = "".join(letters)[:5]
        if len(key) >= 2:
            return key
    base = re.sub(r"[^A-Za-z]", "", words[0]) if words else ""
    key = (base[:3] or "PRJ").upper()
    if len(key) < 2:
        key = (key + "PRJ")[:2]
    return key[:5] or "PRJ"


class Store:
    """Load/write one project's issues. Stateless beyond ``path`` — safe to
    build a fresh one per call (``Store()`` below), same as
    ``question_store.Store``."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_path()

    @contextmanager
    def _db(self, *, write: bool = False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = self.path.resolve().as_uri() + ("?mode=rwc" if write else "?mode=ro")
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and write:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise BoardError("unrecognized board database", "board.bad_database")
                db.executescript(
                    """
                    CREATE TABLE issues (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        body_md TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        priority TEXT NOT NULL,
                        assignee TEXT NOT NULL DEFAULT '',
                        labels_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        closed_at TEXT,
                        created_by TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX issues_project ON issues(project_id, status);
                    CREATE TABLE issue_counters (
                        project_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        next_seq INTEGER NOT NULL,
                        PRIMARY KEY (project_id, key)
                    );
                    CREATE TABLE comments (
                        id TEXT PRIMARY KEY,
                        issue_id TEXT NOT NULL,
                        author TEXT NOT NULL DEFAULT '',
                        body_md TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX comments_issue ON comments(issue_id, created_at);
                    CREATE TABLE events (
                        id TEXT PRIMARY KEY,
                        issue_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        actor TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX events_issue ON events(issue_id, created_at);
                    CREATE TABLE links (
                        id TEXT PRIMARY KEY,
                        issue_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        target_issue_id TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX links_issue ON links(issue_id);
                    CREATE TABLE refs (
                        id TEXT PRIMARY KEY,
                        issue_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        value TEXT NOT NULL,
                        label TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX refs_issue ON refs(issue_id);
                    """
                )
                db.execute(f"PRAGMA user_version={VERSION}")
            elif version != VERSION:
                raise BoardError("unsupported board database version", "board.bad_database")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Decoding
    # ------------------------------------------------------------------
    @staticmethod
    def _decode_issue(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "seq": row["seq"],
            "type": row["type"],
            "title": row["title"],
            "body_md": row["body_md"],
            "status": row["status"],
            "status_category": STATUS_CATEGORY.get(row["status"], "unstarted"),
            "priority": row["priority"],
            "assignee": row["assignee"] or "",
            "labels": json.loads(row["labels_json"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "closed_at": row["closed_at"],
            "created_by": row["created_by"] or "",
        }

    @staticmethod
    def _decode_comment(row: sqlite3.Row) -> Dict[str, Any]:
        return {"id": row["id"], "issue_id": row["issue_id"], "author": row["author"],
                "body_md": row["body_md"], "created_at": row["created_at"]}

    @staticmethod
    def _decode_event(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        return {"id": row["id"], "issue_id": row["issue_id"], "kind": row["kind"],
                "payload": payload, "actor": row["actor"], "created_at": row["created_at"]}

    @staticmethod
    def _decode_link(row: sqlite3.Row) -> Dict[str, Any]:
        return {"id": row["id"], "issue_id": row["issue_id"], "kind": row["kind"],
                "target_issue_id": row["target_issue_id"], "created_at": row["created_at"]}

    @staticmethod
    def _decode_ref(row: sqlite3.Row) -> Dict[str, Any]:
        return {"id": row["id"], "issue_id": row["issue_id"], "kind": row["kind"],
                "value": row["value"], "label": row["label"], "created_at": row["created_at"]}

    @staticmethod
    def _compact(issue: Dict[str, Any], blocked_by: Sequence[str]) -> Dict[str, Any]:
        return {
            "id": issue["id"], "type": issue["type"], "title": issue["title"],
            "status": issue["status"], "priority": issue["priority"],
            "assignee": issue["assignee"], "labels": issue["labels"],
            "updated_at": issue["updated_at"], "blocked_by": list(blocked_by),
        }

    # ------------------------------------------------------------------
    # Ids
    # ------------------------------------------------------------------
    def _next_id(self, db: sqlite3.Connection, project_id: str, key: str) -> Tuple[str, int]:
        """Allocate the next `KEY-N` id. Runs inside the caller's write
        transaction (`BEGIN IMMEDIATE` in `_db`), so the read-then-write here
        is atomic against another writer on this same database file — the
        "next_seq bajo lock" CONTRATO_BOARD asks for.

        `issues.id` is this table's primary key, and every lookup (`get`,
        `update_issue`, `claim`, `add_comment`, ...) takes the bare id with no
        project qualifier — the same shape the routes and agent tools already
        use (`board_get(id)`, never `board_get(project, id)`), because a
        project's board key is meant to be unique per user
        (`ProjectStore.set_board_key` enforces exactly that going forward).
        Two projects that never customized their default DERIVED key
        (`derive_key`) could still collide on a short name — "Faustus" and
        "Faustino" both derive "FAU" — so the per-project counter here is
        also checked against every OTHER project's issues before it is used:
        on a collision the sequence skips ahead (never reused, never
        reassigned) until it lands on a genuinely free id, keeping ids
        globally unique without renumbering anything that already exists."""
        key = str(key or "").strip().upper()
        if not key_valid(key):
            raise BoardError(f"invalid board key: {key!r}", "board.invalid_key")
        row = db.execute(
            "SELECT next_seq FROM issue_counters WHERE project_id=? AND key=?",
            (project_id, key),
        ).fetchone()
        seq = int(row["next_seq"]) if row is not None else 1
        while db.execute("SELECT 1 FROM issues WHERE id=?", (f"{key}-{seq}",)).fetchone():
            seq += 1
        if row is None:
            db.execute(
                "INSERT INTO issue_counters (project_id, key, next_seq) VALUES (?, ?, ?)",
                (project_id, key, seq + 1),
            )
        else:
            db.execute(
                "UPDATE issue_counters SET next_seq=? WHERE project_id=? AND key=?",
                (seq + 1, project_id, key),
            )
        return f"{key}-{seq}", seq

    # ------------------------------------------------------------------
    # Events / links (internal, called inside an open write transaction)
    # ------------------------------------------------------------------
    def _insert_event(self, db: sqlite3.Connection, issue_id: str, kind: str,
                       payload: Dict[str, Any], actor: str) -> None:
        db.execute(
            "INSERT INTO events (id, issue_id, kind, payload_json, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"evt_{uuid.uuid4().hex[:16]}", issue_id, kind, _json(payload), actor or "", _now()),
        )

    def _link_exists(self, db: sqlite3.Connection, issue_id: str, kind: str, target: str) -> bool:
        return db.execute(
            "SELECT 1 FROM links WHERE issue_id=? AND kind=? AND target_issue_id=?",
            (issue_id, kind, target),
        ).fetchone() is not None

    def _insert_link_row(self, db: sqlite3.Connection, issue_id: str, kind: str, target: str) -> str:
        if self._link_exists(db, issue_id, kind, target):
            row = db.execute(
                "SELECT id FROM links WHERE issue_id=? AND kind=? AND target_issue_id=?",
                (issue_id, kind, target),
            ).fetchone()
            return row["id"]
        link_id = f"lnk_{uuid.uuid4().hex[:16]}"
        db.execute(
            "INSERT INTO links (id, issue_id, kind, target_issue_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (link_id, issue_id, kind, target, _now()),
        )
        return link_id

    def _add_link_pair(self, db: sqlite3.Connection, issue_id: str, kind: str, target: str) -> str:
        if kind not in LINK_KINDS:
            raise BoardError(f"unknown link kind: {kind!r}", "board.invalid_link")
        if not db.execute("SELECT 1 FROM issues WHERE id=?", (target,)).fetchone():
            raise NotFoundError(f"link target {target!r} does not exist")
        link_id = self._insert_link_row(db, issue_id, kind, target)
        mirror = _MIRROR_KIND.get(kind)
        if mirror:
            self._insert_link_row(db, target, mirror, issue_id)
        return link_id

    def _open_blockers(self, db: sqlite3.Connection, issue_id: str) -> List[str]:
        """Ids of `blocked_by` targets that are NOT in a terminal status —
        what actually keeps this issue out of `ready` (a blocker that is
        itself done/wontfix/duplicate no longer blocks anything)."""
        rows = db.execute(
            "SELECT l.target_issue_id AS tid, i.status AS status "
            "FROM links l JOIN issues i ON i.id = l.target_issue_id "
            "WHERE l.issue_id=? AND l.kind='blocked_by'",
            (issue_id,),
        ).fetchall()
        return [r["tid"] for r in rows if r["status"] not in TERMINAL_STATUSES]

    # ------------------------------------------------------------------
    # Create / read
    # ------------------------------------------------------------------
    def create_issue(
        self, project_id: str, key: str, *, type: str, title: str, body_md: str = "",
        priority: str = DEFAULT_PRIORITY, assignee: str = "", labels: Optional[List[str]] = None,
        status: str = DEFAULT_STATUS, created_by: str = "",
        links: Optional[List[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        project_id = str(project_id or "").strip()
        if not project_id:
            raise BoardError("project_id is required")
        title = (title or "").strip()
        if not title:
            raise BoardError("title is required")
        type = str(type or DEFAULT_TYPE).strip().lower()
        if type not in TYPES:
            raise BoardError(f"invalid issue type: {type!r} (one of {TYPES})")
        priority = str(priority or DEFAULT_PRIORITY).strip().upper()
        if priority not in PRIORITIES:
            raise BoardError(f"invalid priority: {priority!r} (one of {PRIORITIES})")
        status = str(status or DEFAULT_STATUS).strip().lower()
        if status not in STATUSES:
            raise BoardError(f"invalid status: {status!r} (one of {STATUSES})")
        body_md = str(body_md or "")
        if len(body_md.encode("utf-8")) > MAX_BODY_BYTES:
            raise BoardError(f"body_md exceeds {MAX_BODY_BYTES} bytes", "board.too_large")

        with self._db(write=True) as db:
            issue_id, seq = self._next_id(db, project_id, key)
            stamp = _now()
            closed_at = stamp if status in TERMINAL_STATUSES else None
            db.execute(
                "INSERT INTO issues (id, project_id, seq, type, title, body_md, status, priority, "
                "assignee, labels_json, created_at, updated_at, closed_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (issue_id, project_id, seq, type, title, body_md, status, priority,
                 str(assignee or ""), _json(_labels(labels)), stamp, stamp, closed_at,
                 str(created_by or "")),
            )
            self._insert_event(db, issue_id, "created", {"type": type, "title": title}, created_by)
            for raw in links or []:
                kind = str((raw or {}).get("kind") or "").strip().lower()
                target = str((raw or {}).get("target") or "").strip()
                if not kind or not target:
                    continue
                self._add_link_pair(db, issue_id, kind, target)
        return self.get(issue_id)  # type: ignore[return-value]

    def get(self, issue_id: str) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                return None
            issue = self._decode_issue(row)
            issue["comments"] = [self._decode_comment(r) for r in db.execute(
                "SELECT * FROM comments WHERE issue_id=? ORDER BY created_at", (issue_id,))]
            issue["events"] = [self._decode_event(r) for r in db.execute(
                "SELECT * FROM events WHERE issue_id=? ORDER BY created_at", (issue_id,))]
            issue["links"] = [self._decode_link(r) for r in db.execute(
                "SELECT * FROM links WHERE issue_id=? ORDER BY created_at", (issue_id,))]
            issue["refs"] = [self._decode_ref(r) for r in db.execute(
                "SELECT * FROM refs WHERE issue_id=? ORDER BY created_at", (issue_id,))]
            issue["blocked_by"] = self._open_blockers(db, issue_id)
        return issue

    def get_or_raise(self, issue_id: str) -> Dict[str, Any]:
        issue = self.get(issue_id)
        if issue is None:
            raise NotFoundError()
        return issue

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    def list_issues(
        self, project_id: str, *, status: str = "", type: str = "", assignee: str = "",
        q: str = "", priority: str = "", label: str = "", limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if not self.path.exists():
            return [], None
        limit = max(1, min(int(limit or 50), 200))
        try:
            offset = max(0, int(cursor)) if cursor else 0
        except (TypeError, ValueError):
            offset = 0
        clauses = ["project_id = ?"]
        params: List[Any] = [project_id]
        for field, value in (("status", status), ("type", type), ("priority", priority)):
            value = str(value or "").strip()
            if value:
                clauses.append(f"{field} = ?")
                params.append(value.lower() if field != "priority" else value.upper())
        assignee = str(assignee or "").strip()
        if assignee:
            clauses.append("assignee = ?")
            params.append(assignee)
        with self._db() as db:
            rows = db.execute(
                f"SELECT * FROM issues WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC, seq DESC",
                params,
            ).fetchall()
            issues = [self._decode_issue(r) for r in rows]
            needle = str(q or "").strip().casefold()
            if needle:
                issues = [i for i in issues if needle in i["title"].casefold()
                          or needle in i["body_md"].casefold()]
            label = str(label or "").strip()
            if label:
                issues = [i for i in issues if label in i["labels"]]
            total = len(issues)
            page = issues[offset:offset + limit]
            compact = [self._compact(issue, self._open_blockers(db, issue["id"])) for issue in page]
        next_cursor = str(offset + limit) if offset + limit < total else None
        return compact, next_cursor

    def ready_issues(self, project_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """open/in_progress issues with no open `blocked_by` — Beads' `bd
        ready` (BOARD_RESEARCH.md): the caller does not reason over the whole
        backlog, this decides it. Ordered by priority, then age (oldest
        first)."""
        if not self.path.exists():
            return []
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM issues WHERE project_id=? AND status IN ('open','in_progress') "
                "ORDER BY created_at ASC",
                (project_id,),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for row in rows:
                issue = self._decode_issue(row)
                blockers = self._open_blockers(db, issue["id"])
                if blockers:
                    continue
                out.append(self._compact(issue, []))
        out.sort(key=lambda c: _PRIORITY_ORDER.get(c["priority"], 9))
        return out[: max(1, int(limit or 50))]

    def summary(self, project_id: str) -> Dict[str, Any]:
        counts = {s: 0 for s in STATUSES}
        if not self.path.exists():
            return {"counts": counts, "ready": [], "in_progress": [], "recent_done": []}
        with self._db() as db:
            for row in db.execute(
                "SELECT status, COUNT(*) AS n FROM issues WHERE project_id=? GROUP BY status",
                (project_id,),
            ):
                counts[row["status"]] = row["n"]
            in_progress_rows = db.execute(
                "SELECT * FROM issues WHERE project_id=? AND status='in_progress' "
                "ORDER BY updated_at DESC LIMIT 20",
                (project_id,),
            ).fetchall()
            in_progress = [self._compact(self._decode_issue(r), self._open_blockers(db, r["id"]))
                            for r in in_progress_rows]
            done_rows = db.execute(
                "SELECT * FROM issues WHERE project_id=? AND status='done' "
                "ORDER BY closed_at DESC LIMIT 5",
                (project_id,),
            ).fetchall()
            recent_done = [self._compact(self._decode_issue(r), []) for r in done_rows]
        return {
            "counts": counts,
            "ready": self.ready_issues(project_id, limit=8),
            "in_progress": in_progress,
            "recent_done": recent_done,
        }

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    _PATCHABLE = ("title", "body_md", "type", "status", "priority", "assignee", "labels")

    def _validate_transition(self, old: str, new: str) -> None:
        if old == new:
            return
        if old in TERMINAL_STATUSES and new not in ("open", "in_progress"):
            raise InvalidTransitionError(
                f"{old} is terminal; only reopening to 'open' or 'in_progress' is allowed "
                f"(got {new!r})"
            )

    def update_issue(self, issue_id: str, patch: Mapping[str, Any], *, actor: str = "") -> Dict[str, Any]:
        unknown = [k for k in patch if k not in self._PATCHABLE]
        if unknown:
            raise BoardError(f"not a patchable field: {', '.join(sorted(unknown))}")
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                raise NotFoundError()
            updates: Dict[str, Any] = {}
            changed: Dict[str, Any] = {}
            if "status" in patch:
                new_status = str(patch["status"] or "").strip().lower()
                if new_status not in STATUSES:
                    raise BoardError(f"invalid status: {new_status!r} (one of {STATUSES})")
                self._validate_transition(row["status"], new_status)
                if new_status != row["status"]:
                    updates["status"] = new_status
                    updates["closed_at"] = _now() if new_status in TERMINAL_STATUSES else None
                    changed["status"] = {"from": row["status"], "to": new_status}
            if "type" in patch:
                new_type = str(patch["type"] or "").strip().lower()
                if new_type not in TYPES:
                    raise BoardError(f"invalid issue type: {new_type!r} (one of {TYPES})")
                if new_type != row["type"]:
                    updates["type"] = new_type
                    changed["type"] = {"from": row["type"], "to": new_type}
            if "priority" in patch:
                new_priority = str(patch["priority"] or "").strip().upper()
                if new_priority not in PRIORITIES:
                    raise BoardError(f"invalid priority: {new_priority!r} (one of {PRIORITIES})")
                if new_priority != row["priority"]:
                    updates["priority"] = new_priority
                    changed["priority"] = {"from": row["priority"], "to": new_priority}
            if "title" in patch:
                new_title = str(patch["title"] or "").strip()
                if not new_title:
                    raise BoardError("title cannot be empty")
                if new_title != row["title"]:
                    updates["title"] = new_title
                    changed["title"] = True
            if "body_md" in patch:
                new_body = str(patch["body_md"] or "")
                if len(new_body.encode("utf-8")) > MAX_BODY_BYTES:
                    raise BoardError(f"body_md exceeds {MAX_BODY_BYTES} bytes", "board.too_large")
                if new_body != row["body_md"]:
                    updates["body_md"] = new_body
                    changed["body_md"] = True
            if "assignee" in patch:
                new_assignee = str(patch["assignee"] or "").strip()
                if new_assignee != (row["assignee"] or ""):
                    updates["assignee"] = new_assignee
                    changed["assignee"] = {"from": row["assignee"] or "", "to": new_assignee}
            if "labels" in patch:
                new_labels = _labels(patch["labels"])
                if new_labels != json.loads(row["labels_json"] or "[]"):
                    updates["labels_json"] = _json(new_labels)
                    changed["labels"] = True
            if updates:
                updates["updated_at"] = _now()
                set_clause = ", ".join(f"{k}=?" for k in updates)
                db.execute(f"UPDATE issues SET {set_clause} WHERE id=?",
                           (*updates.values(), issue_id))
                if "status" in changed:
                    self._insert_event(db, issue_id, "status_change", changed["status"], actor)
                other_changed = {k: v for k, v in changed.items() if k != "status"}
                if other_changed:
                    self._insert_event(db, issue_id, "edited", other_changed, actor)
        return self.get_or_raise(issue_id)

    def claim(self, issue_id: str, assignee: str, *, actor: str = "") -> Dict[str, Any]:
        """Atomic claim: succeeds when the issue is unclaimed, already open/
        blocked, or already claimed by THIS SAME assignee (idempotent
        re-claim); refuses (`board.claimed`) when another assignee holds it.
        Runs inside one write transaction, so two agents racing to claim the
        same issue serialize on SQLite's own file lock rather than both
        succeeding."""
        assignee = str(assignee or "").strip()
        if not assignee:
            raise BoardError("assignee is required")
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                raise NotFoundError()
            holder = row["assignee"] or ""
            if row["status"] == "in_progress" and holder and holder != assignee:
                raise ClaimedError(f"already claimed by {holder!r}", holder)
            stamp = _now()
            db.execute(
                "UPDATE issues SET status='in_progress', assignee=?, updated_at=? WHERE id=?",
                (assignee, stamp, issue_id),
            )
            self._insert_event(db, issue_id, "claimed", {"assignee": assignee}, actor or assignee)
        return self.get_or_raise(issue_id)

    def add_comment(self, issue_id: str, body_md: str, *, author: str = "") -> Dict[str, Any]:
        body_md = str(body_md or "").strip()
        if not body_md:
            raise BoardError("comment body is required")
        if len(body_md.encode("utf-8")) > MAX_BODY_BYTES:
            raise BoardError(f"comment exceeds {MAX_BODY_BYTES} bytes", "board.too_large")
        with self._db(write=True) as db:
            if not db.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone():
                raise NotFoundError()
            comment_id = f"cmt_{uuid.uuid4().hex[:16]}"
            stamp = _now()
            db.execute(
                "INSERT INTO comments (id, issue_id, author, body_md, created_at) VALUES (?, ?, ?, ?, ?)",
                (comment_id, issue_id, author or "", body_md, stamp),
            )
            self._insert_event(db, issue_id, "comment", {"comment_id": comment_id}, author)
            db.execute("UPDATE issues SET updated_at=? WHERE id=?", (stamp, issue_id))
        with self._db() as db:
            row = db.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        return self._decode_comment(row)

    def add_link(self, issue_id: str, kind: str, target: str, *, actor: str = "") -> Dict[str, Any]:
        kind = str(kind or "").strip().lower()
        target = str(target or "").strip()
        if kind not in LINK_KINDS:
            raise BoardError(f"unknown link kind: {kind!r} (one of {LINK_KINDS})", "board.invalid_link")
        if not target:
            raise BoardError("target is required", "board.invalid_link")
        if target == issue_id:
            raise BoardError("an issue cannot link to itself", "board.invalid_link")
        with self._db(write=True) as db:
            if not db.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone():
                raise NotFoundError()
            link_id = self._add_link_pair(db, issue_id, kind, target)
            self._insert_event(db, issue_id, "link_added", {"kind": kind, "target": target}, actor)
        with self._db() as db:
            row = db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()
        return self._decode_link(row)

    def remove_link(self, issue_id: str, link_id: str) -> bool:
        with self._db(write=True) as db:
            row = db.execute(
                "SELECT * FROM links WHERE id=? AND issue_id=?", (link_id, issue_id)
            ).fetchone()
            if row is None:
                return False
            db.execute("DELETE FROM links WHERE id=?", (link_id,))
            mirror = _MIRROR_KIND.get(row["kind"])
            if mirror:
                db.execute(
                    "DELETE FROM links WHERE issue_id=? AND kind=? AND target_issue_id=?",
                    (row["target_issue_id"], mirror, issue_id),
                )
        return True

    def add_ref(self, issue_id: str, kind: str, value: str, *, label: str = "") -> Dict[str, Any]:
        kind = str(kind or "").strip().lower()
        value = str(value or "").strip()
        if kind not in REF_KINDS:
            raise BoardError(f"unknown ref kind: {kind!r} (one of {REF_KINDS})", "board.invalid_ref")
        if not value:
            raise BoardError("value is required", "board.invalid_ref")
        with self._db(write=True) as db:
            if not db.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone():
                raise NotFoundError()
            # Idempotent: the same (issue, kind, value) ref — most often a
            # commit sha mentioned more than once — is not duplicated.
            existing = db.execute(
                "SELECT * FROM refs WHERE issue_id=? AND kind=? AND value=?",
                (issue_id, kind, value),
            ).fetchone()
            if existing is not None:
                return self._decode_ref(existing)
            ref_id = f"ref_{uuid.uuid4().hex[:16]}"
            db.execute(
                "INSERT INTO refs (id, issue_id, kind, value, label, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ref_id, issue_id, kind, value, str(label or "")[:300], _now()),
            )
        with self._db() as db:
            row = db.execute("SELECT * FROM refs WHERE id=?", (ref_id,)).fetchone()
        return self._decode_ref(row)

    def delete_issue(self, issue_id: str) -> bool:
        with self._db(write=True) as db:
            row = db.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                return False
            db.execute("DELETE FROM issues WHERE id=?", (issue_id,))
            db.execute("DELETE FROM comments WHERE issue_id=?", (issue_id,))
            db.execute("DELETE FROM events WHERE issue_id=?", (issue_id,))
            db.execute("DELETE FROM refs WHERE issue_id=?", (issue_id,))
            db.execute("DELETE FROM links WHERE issue_id=? OR target_issue_id=?", (issue_id, issue_id))
        return True

    # ------------------------------------------------------------------
    # Git hook (src/agent_git_policy.py::after_turn, src/git_panel.py::commit)
    # ------------------------------------------------------------------
    def link_commit(self, project_id: str, sha: str, message: str) -> Dict[str, Any]:
        """Scan a commit message for this project's issue ids: every mention
        becomes a `refs` row (kind='commit'); a mention preceded by a magic
        close word (see `_CLOSE_RE`) also moves the issue to `done` (unless
        it is already terminal) with a `commit_linked` event carrying the
        sha. Never raises — a malformed message or a missing project simply
        links nothing; this runs on the hot commit path and must not turn a
        successful `git commit` into a failure."""
        sha = str(sha or "").strip()
        message = str(message or "")
        project_id = str(project_id or "").strip()
        linked: List[str] = []
        closed: List[str] = []
        if not sha or not message or not project_id or not self.path.exists():
            return {"linked": linked, "closed": closed}
        try:
            mentioned = {m.group(1).upper() for m in _ISSUE_ID_RE.finditer(message)}
            closing = {m.group(1).upper() for m in _CLOSE_RE.finditer(message)}
            if not mentioned:
                return {"linked": linked, "closed": closed}
            with self._db(write=True) as db:
                for issue_id in mentioned:
                    row = db.execute(
                        "SELECT * FROM issues WHERE id=? AND project_id=?",
                        (issue_id, project_id),
                    ).fetchone()
                    if row is None:
                        continue
                    stamp = _now()
                    existing = db.execute(
                        "SELECT 1 FROM refs WHERE issue_id=? AND kind='commit' AND value=?",
                        (issue_id, sha),
                    ).fetchone()
                    if existing is None:
                        db.execute(
                            "INSERT INTO refs (id, issue_id, kind, value, label, created_at) "
                            "VALUES (?, ?, 'commit', ?, ?, ?)",
                            (f"ref_{uuid.uuid4().hex[:16]}", issue_id, sha,
                             message.strip().splitlines()[0][:300] if message.strip() else "",
                             stamp),
                        )
                    linked.append(issue_id)
                    if issue_id in closing and row["status"] not in TERMINAL_STATUSES:
                        db.execute(
                            "UPDATE issues SET status='done', closed_at=?, updated_at=? WHERE id=?",
                            (stamp, stamp, issue_id),
                        )
                        self._insert_event(db, issue_id, "commit_linked", {"sha": sha}, "git")
                        closed.append(issue_id)
        except Exception:  # noqa: BLE001 - never break a commit over this
            logger.debug("project_board.link_commit failed for %s/%s", project_id, sha, exc_info=True)
        return {"linked": linked, "closed": closed}

    # ------------------------------------------------------------------
    # Migration from OBJETIVOS.md / PENDIENTES.md / backlog.json
    # ------------------------------------------------------------------
    def import_sources(
        self, project: Mapping[str, Any], key: str, *, sources: Sequence[str],
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Idempotent import: each created issue carries a label
        `import:<source>:<ext_id>` and a second pass with the same source
        skips anything that label already marks as imported — a re-run (or a
        `dry_run` preview) never duplicates issues."""
        project_id = str(project.get("id") or "")
        workspace = str(project.get("workspace") or "").strip()
        created = 0
        skipped = 0
        preview: List[Dict[str, Any]] = []
        if not project_id or not workspace:
            return {"created": 0, "skipped": 0, "preview": []}

        existing_labels: set = set()
        if self.path.exists():
            with self._db() as db:
                for row in db.execute(
                    "SELECT labels_json FROM issues WHERE project_id=?", (project_id,)
                ):
                    for lab in json.loads(row["labels_json"] or "[]"):
                        existing_labels.add(lab)

        def _emit(source: str, ext_id: str, *, type: str, title: str, body: str,
                   status: str, priority: str, extra_labels: Sequence[str] = ()) -> None:
            nonlocal created, skipped
            label = f"import:{source}:{ext_id}"
            if label in existing_labels:
                skipped += 1
                return
            item = {"source": source, "ext_id": ext_id, "type": type, "title": title,
                    "status": status, "priority": priority}
            preview.append(item)
            if dry_run:
                return
            self.create_issue(
                project_id, key, type=type, title=title, body_md=body, priority=priority,
                status=status, created_by="import", labels=[label, *extra_labels],
            )
            existing_labels.add(label)
            created += 1

        if "objetivos" in sources:
            for obj in _parse_objetivos(os.path.join(workspace, "OBJETIVOS.md")):
                _emit("objetivos", obj["ext_id"], type="feature", title=obj["title"],
                      body=obj["body"], status="done" if obj["done"] else "open",
                      priority=DEFAULT_PRIORITY)

        if "pendientes" in sources:
            for item in _parse_pendientes(os.path.join(workspace, "PENDIENTES.md")):
                _emit("pendientes", item["ext_id"], type="task", title=item["title"],
                      body="", status="done" if item["done"] else "open",
                      priority=DEFAULT_PRIORITY)

        if "backlog" in sources:
            backlog_path = os.path.join(workspace, "docs", "spec", "v2", "backlog.json")
            id_map: Dict[str, str] = {}
            items = _parse_backlog(backlog_path)
            for item in items:
                priority = item.get("priority") or DEFAULT_PRIORITY
                if priority not in PRIORITIES:
                    priority = DEFAULT_PRIORITY
                status_text = str(item.get("status") or "").strip().lower()
                status = "done" if status_text in ("done", "closed", "complete", "completed") else "open"
                before = created
                _emit("backlog", item["id"], type="feature", title=item.get("title") or item["id"],
                      body="", status=status, priority=priority,
                      extra_labels=[f"spec:{item['id']}"])
                if not dry_run and created > before:
                    fresh = self.list_issues(project_id, label=f"import:backlog:{item['id']}", limit=1)[0]
                    if fresh:
                        id_map[item["id"]] = fresh[0]["id"]
            if not dry_run:
                for item in items:
                    src_id = id_map.get(item["id"])
                    if not src_id:
                        continue
                    for dep in item.get("depends_on") or []:
                        target = id_map.get(dep)
                        if target:
                            try:
                                self.add_link(src_id, "blocked_by", target, actor="import")
                            except BoardError:
                                pass

        return {"created": created, "skipped": skipped, "preview": preview}

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_markdown(self, project_id: str, key: str, project_name: str) -> str:
        lines = [f"# {project_name or 'Project'} board ({key})", ""]
        if not self.path.exists():
            return "\n".join(lines) + "\n"
        with self._db() as db:
            for status in STATUSES:
                rows = db.execute(
                    "SELECT * FROM issues WHERE project_id=? AND status=? ORDER BY seq",
                    (project_id, status),
                ).fetchall()
                if not rows:
                    continue
                lines.append(f"## {status} ({len(rows)})")
                for row in rows:
                    issue = self._decode_issue(row)
                    mark = "x" if status in TERMINAL_STATUSES else " "
                    lines.append(f"- [{mark}] {issue['id']} [{issue['priority']}] {issue['type']}: {issue['title']}")
                lines.append("")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Parsers for /import (BOARD_RESEARCH.md "Migración")
# ---------------------------------------------------------------------------
_OBJ_HEADING_RE = re.compile(r"^##\s+OBJ-(\d+)\s*[·:.\-]?\s*(.*)$")
_CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.+)$")
_STRIKETHROUGH_LINE_RE = re.compile(r"^\s*[-*]\s*~~(.+)~~\s*$")
_RULES_HEADING_RE = re.compile(r"regla", re.IGNORECASE)


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _parse_objetivos(path: str) -> List[Dict[str, Any]]:
    text = _read_text(path)
    if not text:
        return []
    out: List[Dict[str, Any]] = []
    lines = text.splitlines()
    current: Optional[Dict[str, Any]] = None
    body_lines: List[str] = []
    for line in lines:
        m = _OBJ_HEADING_RE.match(line)
        if m or line.startswith("## "):
            if current is not None:
                current["body"] = "\n".join(body_lines).strip()
                out.append(current)
            body_lines = []
            if m:
                num, rest = m.group(1), m.group(2).strip()
                done = rest.startswith("~~") and rest.endswith("~~")
                title = rest.strip("~").strip() or f"OBJ-{num}"
                current = {"ext_id": f"OBJ-{num}", "title": title, "done": done}
            else:
                current = None
        elif current is not None:
            body_lines.append(line)
            if re.search(r"\b(done|completed|completado|cerrado)\b", line, re.IGNORECASE):
                current["done"] = True
    if current is not None:
        current["body"] = "\n".join(body_lines).strip()
        out.append(current)
    return out


def _parse_pendientes(path: str) -> List[Dict[str, Any]]:
    text = _read_text(path)
    if not text:
        return []
    out: List[Dict[str, Any]] = []
    in_rules_section = False
    for line in text.splitlines():
        if line.startswith("#"):
            in_rules_section = bool(_RULES_HEADING_RE.search(line))
            continue
        if in_rules_section:
            # A permanent rule ("never run two large models at once") is not
            # a task (BOARD_RESEARCH.md "Migración"); skip the whole section.
            continue
        m = _CHECKLIST_RE.match(line)
        st = _STRIKETHROUGH_LINE_RE.match(line)
        if m:
            done = m.group(1).strip().lower() == "x"
            title = m.group(2).strip()
        elif st:
            done = True
            title = st.group(1).strip()
        else:
            continue
        if not title:
            continue
        ext_id = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
        out.append({"ext_id": ext_id, "title": title[:300], "done": done})
    return out


def _parse_backlog(path: str) -> List[Dict[str, Any]]:
    text = _read_text(path)
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("items") or data.get("backlog") or []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        ext_id = str(raw.get("id") or "").strip()
        if not ext_id:
            continue
        out.append({
            "id": ext_id,
            "title": str(raw.get("title") or raw.get("name") or ext_id).strip(),
            "priority": str(raw.get("priority") or "").strip().upper(),
            "status": raw.get("status"),
            "depends_on": raw.get("depends_on") or [],
        })
    return out


# ---------------------------------------------------------------------------
# Module-level convenience over the default store (mirrors question_store.py)
# ---------------------------------------------------------------------------
def create_issue(project_id: str, key: str, **kwargs: Any) -> Dict[str, Any]:
    return Store().create_issue(project_id, key, **kwargs)


def get(issue_id: str) -> Optional[Dict[str, Any]]:
    return Store().get(issue_id)


def get_or_raise(issue_id: str) -> Dict[str, Any]:
    return Store().get_or_raise(issue_id)


def list_issues(project_id: str, **kwargs: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    return Store().list_issues(project_id, **kwargs)


def ready_issues(project_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return Store().ready_issues(project_id, limit=limit)


def summary(project_id: str) -> Dict[str, Any]:
    return Store().summary(project_id)


def update_issue(issue_id: str, patch: Mapping[str, Any], *, actor: str = "") -> Dict[str, Any]:
    return Store().update_issue(issue_id, patch, actor=actor)


def claim(issue_id: str, assignee: str, *, actor: str = "") -> Dict[str, Any]:
    return Store().claim(issue_id, assignee, actor=actor)


def add_comment(issue_id: str, body_md: str, *, author: str = "") -> Dict[str, Any]:
    return Store().add_comment(issue_id, body_md, author=author)


def add_link(issue_id: str, kind: str, target: str, *, actor: str = "") -> Dict[str, Any]:
    return Store().add_link(issue_id, kind, target, actor=actor)


def remove_link(issue_id: str, link_id: str) -> bool:
    return Store().remove_link(issue_id, link_id)


def add_ref(issue_id: str, kind: str, value: str, *, label: str = "") -> Dict[str, Any]:
    return Store().add_ref(issue_id, kind, value, label=label)


def delete_issue(issue_id: str) -> bool:
    return Store().delete_issue(issue_id)


def link_commit(project_id: str, sha: str, message: str) -> Dict[str, Any]:
    return Store().link_commit(project_id, sha, message)


def import_sources(project: Mapping[str, Any], key: str, *, sources: Sequence[str],
                    dry_run: bool = True) -> Dict[str, Any]:
    return Store().import_sources(project, key, sources=sources, dry_run=dry_run)


def export_markdown(project_id: str, key: str, project_name: str) -> str:
    return Store().export_markdown(project_id, key, project_name)

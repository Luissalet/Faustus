"""requirements/store.py — versioned requirements linked to a project (ADP-18).

Same shape as ``src.project_board``: self-contained SQLite under ``DATA_DIR``
(``requirements.sqlite3``), own schema, one write transaction per call
(``BEGIN IMMEDIATE``), stateless ``Store`` safe to build fresh per call. A
project's requirements are disposable relative to the rest of the app the
same way a project's board is -- a corrupt/deleted file loses a spec, never a
session, a document or an account.

This deliberately does NOT touch ``src.project_board``: a requirement is not
an issue (BOARD_RESEARCH.md's vocabulary is closed by design, per
CONTRATO_ADP_W1 rule "no ampliar el vocabulario del board"). A requirement
can be *linked* from an issue (via the issue's own ``refs``/``links``) but
this module never writes into ``board.sqlite3``.

Identity: a requirement's readable id is ``REQ-N``, sequential PER PROJECT
(a fresh counter per ``project_id`` -- unlike the board's globally-unique
``FAU-12``, "REQ-1" is expected to repeat across different projects and must
never collide, because the primary key stored here is ``project_id + key``,
never ``key`` alone). Every public method therefore takes ``project_id``
explicitly.

Versioning: every mutation that changes title/text/acceptance/status/source
appends a new, immutable row to ``requirement_revisions`` and bumps
``current_revision`` on the requirement -- the past revision row is never
edited or deleted (ADP-18 acceptance: "cambio de requisito crea revisión y
no modifica el pasado").

Proposals vs decisions: ``proposed_by='model'`` requirements are born
``status='proposed'`` no matter what the caller asked for, and
:meth:`Store.update` refuses (`requirements.model_cannot_decide`) a
``by='model'`` caller trying to move ``status`` to ``accepted`` or
``rejected`` -- "solo un humano acepta" (ADP-18 limits) is enforced here,
not left to callers to remember.
"""

from __future__ import annotations

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
MAX_TEXT_BYTES = 200_000

SOURCES = ("doc", "issue", "url", "human")
STATUSES = ("proposed", "accepted", "rejected", "superseded")
PROPOSED_BY = ("human", "model")
LINK_KINDS = ("implements", "tests", "evidences", "issue")
LINK_STATES = ("linked", "needs_review", "stale", "unknown")
#: Decisions a model is never allowed to make for itself (ADP-18 limits:
#: "no tratar una propuesta de modelo como decisión humana").
_HUMAN_ONLY_STATUSES = ("accepted", "rejected")

_KEY_RE = re.compile(r"^REQ-\d+$")


class RequirementsError(ValueError):
    """Base error. ``error_class`` mirrors ``src.project_board.BoardError`` --
    a route turns it into an HTTP body, a tool turns it into a result dict,
    both read the same attribute."""

    def __init__(self, message: str, error_class: str = "requirements.invalid"):
        super().__init__(message)
        self.error_class = error_class


class NotFoundError(RequirementsError):
    def __init__(self, message: str = "Requirement not found"):
        super().__init__(message, "requirements.not_found")


def default_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "requirements.sqlite3"


def _now() -> str:
    from src.contracts.base import now_iso
    return now_iso()


def _json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise RequirementsError("payload too large", "requirements.too_large")
    return text


def _acceptance(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for v in value:
        text = str(v or "").strip()
        if text:
            out.append(text)
    return out[:100]


def key_valid(key: str) -> bool:
    return bool(_KEY_RE.match(str(key or "").strip().upper()))


# ---------------------------------------------------------------------------
# Sidecar (`.faustus/requirements.yaml`) -- a deliberately small, tolerant
# subset of YAML, NOT a general parser. No new dependency: pyyaml is not a
# declared requirement anywhere in this repo (requirements*.txt / pyproject
# were checked before writing this), so a hand-authored sidecar is read with
# a purpose-built scanner instead of adding one. Supports exactly the shape
# a human would write by hand for this:
#
#   requirements:
#     - key: REQ-1
#       title: Users can reset their password
#       source: doc
#       status: accepted
#       text: |
#         A user must be able to request a reset link by email.
#         The link expires after one hour.
#       acceptance:
#         - Reset link expires after 1 hour
#         - Old password stops working once reset completes
#
# Anything outside this shape (flow style `{a: b}`, anchors, multi-document
# files, tabs) is skipped per-line with an entry in `errors`, never raised --
# a malformed sidecar degrades to fewer items, it does not break the read
# path that loads it (same "tolerant" promise the module docstring makes).
# ---------------------------------------------------------------------------
_SIDECAR_ROOT_RE = re.compile(r"^requirements:\s*$")
_SIDECAR_ITEM_RE = re.compile(r"^(?P<indent>[ ]*)-[ ]?(?P<rest>.*)$")
_SIDECAR_FIELD_RE = re.compile(r"^(?P<indent>[ ]*)(?P<field>[A-Za-z_][A-Za-z0-9_]*):[ ]?(?P<value>.*)$")
_SIDECAR_INLINE_FIELD_RE = re.compile(r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*):[ ]?(?P<value>.*)$")
_SIDECAR_LIST_FIELDS = {"acceptance"}
_SIDECAR_BLOCK_FIELDS = {"text"}


def _sidecar_unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_sidecar(text: str) -> Dict[str, Any]:
    """Tolerant parse of the small YAML subset above -> ``{"items": [...],
    "errors": [...]}``. Pure; never raises. Each item is a plain dict with
    whatever of ``key``/``title``/``text``/``source``/``status``/``acceptance``
    it named -- callers decide what a missing field defaults to, this
    function only reports what was actually written down."""
    items: List[Dict[str, Any]] = []
    errors: List[str] = []
    if not text or "\t" in text:
        if text and "\t" in text:
            errors.append("tabs are not supported; use spaces for indentation")
        return {"items": items, "errors": errors}

    lines = text.splitlines()
    i = 0
    n = len(lines)
    # Find the `requirements:` root, or accept a bare top-level list.
    while i < n and not lines[i].strip():
        i += 1
    if i < n and _SIDECAR_ROOT_RE.match(lines[i]):
        i += 1
    elif i < n and _SIDECAR_ITEM_RE.match(lines[i]):
        pass  # bare list at the top of the file
    else:
        errors.append("expected a top-level `requirements:` list")
        return {"items": items, "errors": errors}

    current: Optional[Dict[str, Any]] = None
    item_indent = -1

    def _flush() -> None:
        if current is not None:
            items.append(current)

    def _consume_block(start: int, field_indent: int) -> Tuple[int, str]:
        """Literal block scalar (`field: |`): every following line indented
        MORE than `field_indent` belongs to it, dedented by the first such
        line's own indent -- stops at the first line indented back to
        `field_indent` or less (the next field/item)."""
        j = start + 1
        block_lines: List[str] = []
        block_base: Optional[int] = None
        while j < n:
            bl = lines[j]
            if bl.strip() and len(bl) - len(bl.lstrip(" ")) <= field_indent:
                break
            if bl.strip():
                if block_base is None:
                    block_base = len(bl) - len(bl.lstrip(" "))
                block_lines.append(bl[block_base:] if len(bl) >= block_base else bl.lstrip(" "))
            else:
                block_lines.append("")
            j += 1
        return j, "\n".join(block_lines).rstrip("\n")

    def _consume_list(start: int, field_indent: int) -> Tuple[int, List[str]]:
        """`field:` followed by a nested `- value` list, each indented more
        than `field_indent`."""
        j = start + 1
        values: List[str] = []
        while j < n:
            bl = lines[j]
            if not bl.strip():
                j += 1
                continue
            m_sub = _SIDECAR_ITEM_RE.match(bl)
            if m_sub and len(m_sub.group("indent")) > field_indent:
                values.append(_sidecar_unquote(m_sub.group("rest")))
                j += 1
                continue
            break
        return j, values

    while i < n:
        raw = lines[i]
        if not raw.strip() or raw.strip().startswith("#"):
            i += 1
            continue
        m_item = _SIDECAR_ITEM_RE.match(raw)
        if m_item and (current is None or len(m_item.group("indent")) <= item_indent):
            _flush()
            item_indent = len(m_item.group("indent"))
            current = {}
            rest = m_item.group("rest").strip()
            i += 1
            if rest:
                m_inline = _SIDECAR_INLINE_FIELD_RE.match(rest)
                if m_inline:
                    current[m_inline.group("field")] = _sidecar_unquote(m_inline.group("value"))
                else:
                    errors.append(f"line {i}: could not parse inline item field {rest!r}")
            continue
        m_field = _SIDECAR_FIELD_RE.match(raw)
        if m_field and current is not None and len(m_field.group("indent")) > item_indent:
            field = m_field.group("field")
            value = m_field.group("value")
            field_indent = len(m_field.group("indent"))
            if field in _SIDECAR_BLOCK_FIELDS and value.strip() in ("|", ">"):
                i, text_val = _consume_block(i, field_indent)
                current[field] = text_val
                continue
            if field in _SIDECAR_LIST_FIELDS and not value.strip():
                i, values = _consume_list(i, field_indent)
                current[field] = values
                continue
            current[field] = _sidecar_unquote(value)
            i += 1
            continue
        # A line that matches neither shape at the expected indent -- skip it
        # rather than aborting the whole file.
        errors.append(f"line {i + 1}: could not parse {raw!r}")
        i += 1
    _flush()
    return {"items": items, "errors": errors}


class Store:
    """Load/write requirements across every project -- rows are scoped by
    ``project_id`` in every method, never by a separate per-project file, the
    same call shape ``project_board.Store`` uses."""

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
                    raise RequirementsError("unrecognized requirements database", "requirements.bad_database")
                db.executescript(
                    """
                    CREATE TABLE requirements (
                        req_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        title TEXT NOT NULL,
                        text TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL,
                        acceptance_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL,
                        proposed_by TEXT NOT NULL,
                        current_revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        created_by TEXT NOT NULL DEFAULT ''
                    );
                    CREATE UNIQUE INDEX requirements_project_key ON requirements(project_id, key);
                    CREATE INDEX requirements_project ON requirements(project_id, status);
                    CREATE TABLE req_counters (
                        project_id TEXT PRIMARY KEY,
                        next_seq INTEGER NOT NULL
                    );
                    CREATE TABLE requirement_revisions (
                        id TEXT PRIMARY KEY,
                        req_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        text TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL,
                        acceptance_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL,
                        changed_by TEXT NOT NULL DEFAULT '',
                        change_note TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX revisions_req ON requirement_revisions(req_id, revision);
                    CREATE TABLE requirement_links (
                        id TEXT PRIMARY KEY,
                        req_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        target TEXT NOT NULL,
                        revision TEXT NOT NULL DEFAULT '',
                        req_revision_at_link INTEGER NOT NULL,
                        content_hash TEXT,
                        state TEXT NOT NULL DEFAULT 'linked',
                        created_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX links_req ON requirement_links(req_id);
                    """
                )
                db.execute(f"PRAGMA user_version={VERSION}")
            elif version != VERSION:
                raise RequirementsError("unsupported requirements database version", "requirements.bad_database")
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
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["req_id"], "project_id": row["project_id"], "key": row["key"],
            "title": row["title"], "text": row["text"], "source": row["source"],
            "acceptance": json.loads(row["acceptance_json"] or "[]"),
            "status": row["status"], "proposed_by": row["proposed_by"],
            "current_revision": row["current_revision"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "created_by": row["created_by"] or "",
        }

    @staticmethod
    def _decode_revision(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"], "req_id": row["req_id"], "revision": row["revision"],
            "title": row["title"], "text": row["text"], "source": row["source"],
            "acceptance": json.loads(row["acceptance_json"] or "[]"),
            "status": row["status"], "changed_by": row["changed_by"] or "",
            "change_note": row["change_note"] or "", "created_at": row["created_at"],
        }

    @staticmethod
    def _decode_link(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"], "req_id": row["req_id"], "kind": row["kind"],
            "target": row["target"], "revision": row["revision"] or "",
            "req_revision_at_link": row["req_revision_at_link"],
            "content_hash": row["content_hash"], "state": row["state"],
            "created_by": row["created_by"] or "",
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Ids
    # ------------------------------------------------------------------
    def _next_key(self, db: sqlite3.Connection, project_id: str) -> Tuple[str, int]:
        """Allocate the next ``REQ-N`` for THIS project. Runs inside the
        caller's write transaction, so the read-then-write is atomic against
        another writer on this same database file (same pattern as
        ``project_board.Store._next_id``). Deliberately per-``project_id`` --
        "REQ-1" in two different projects is expected and must not collide
        (ADP-18 acceptance), which is why the row this allocates from is
        keyed on ``project_id`` alone, not shared globally."""
        row = db.execute(
            "SELECT next_seq FROM req_counters WHERE project_id=?", (project_id,)
        ).fetchone()
        seq = int(row["next_seq"]) if row is not None else 1
        if row is None:
            db.execute("INSERT INTO req_counters (project_id, next_seq) VALUES (?, ?)",
                       (project_id, seq + 1))
        else:
            db.execute("UPDATE req_counters SET next_seq=? WHERE project_id=?", (seq + 1, project_id))
        return f"REQ-{seq}", seq

    # ------------------------------------------------------------------
    # Create / read
    # ------------------------------------------------------------------
    def create(
        self, project_id: str, *, title: str, text: str = "", source: str = "human",
        acceptance: Optional[Sequence[str]] = None, proposed_by: str = "human",
        status: Optional[str] = None, created_by: str = "",
    ) -> Dict[str, Any]:
        project_id = str(project_id or "").strip()
        if not project_id:
            raise RequirementsError("project_id is required")
        title = str(title or "").strip()
        if not title:
            raise RequirementsError("title is required")
        source = str(source or "human").strip().lower()
        if source not in SOURCES:
            raise RequirementsError(f"invalid source: {source!r} (one of {SOURCES})")
        proposed_by = str(proposed_by or "human").strip().lower()
        if proposed_by not in PROPOSED_BY:
            raise RequirementsError(f"invalid proposed_by: {proposed_by!r} (one of {PROPOSED_BY})")
        # A model proposal is ALWAYS born `proposed`, regardless of what the
        # caller asked for -- "una propuesta de modelo nace status: proposed;
        # solo un humano acepta" (ADP-18 qué). A human may create a
        # requirement already `accepted` (e.g. importing a decision that was
        # already made elsewhere).
        if proposed_by == "model":
            status = "proposed"
        else:
            status = str(status or "proposed").strip().lower()
        if status not in STATUSES:
            raise RequirementsError(f"invalid status: {status!r} (one of {STATUSES})")
        text = str(text or "")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise RequirementsError(f"text exceeds {MAX_TEXT_BYTES} bytes", "requirements.too_large")
        acc = _acceptance(acceptance)

        with self._db(write=True) as db:
            key, _seq = self._next_key(db, project_id)
            req_id = f"{project_id}:{key}"
            stamp = _now()
            db.execute(
                "INSERT INTO requirements (req_id, project_id, key, title, text, source, "
                "acceptance_json, status, proposed_by, current_revision, created_at, updated_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (req_id, project_id, key, title, text, source, _json(acc), status,
                 proposed_by, stamp, stamp, str(created_by or "")),
            )
            db.execute(
                "INSERT INTO requirement_revisions (id, req_id, revision, title, text, source, "
                "acceptance_json, status, changed_by, change_note, created_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"rev_{uuid.uuid4().hex[:16]}", req_id, title, text, source, _json(acc), status,
                 str(created_by or ""), "created", stamp),
            )
        return self.get(project_id, key)  # type: ignore[return-value]

    def get(self, project_id: str, key: str) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        req_id = f"{str(project_id or '').strip()}:{str(key or '').strip().upper()}"
        with self._db() as db:
            row = db.execute("SELECT * FROM requirements WHERE req_id=?", (req_id,)).fetchone()
            if row is None:
                return None
            reqm = self._decode(row)
            reqm["links"] = [self._decode_link(r) for r in db.execute(
                "SELECT * FROM requirement_links WHERE req_id=? ORDER BY created_at", (req_id,))]
        return reqm

    def get_or_raise(self, project_id: str, key: str) -> Dict[str, Any]:
        reqm = self.get(project_id, key)
        if reqm is None:
            raise NotFoundError(f"{key} not found in project {project_id}")
        return reqm

    def list(self, project_id: str, *, status: str = "", source: str = "", q: str = "") -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        clauses = ["project_id = ?"]
        params: List[Any] = [str(project_id or "")]
        status = str(status or "").strip().lower()
        if status:
            clauses.append("status = ?")
            params.append(status)
        source = str(source or "").strip().lower()
        if source:
            clauses.append("source = ?")
            params.append(source)
        with self._db() as db:
            rows = db.execute(
                f"SELECT * FROM requirements WHERE {' AND '.join(clauses)} ORDER BY key",
                params,
            ).fetchall()
        out = [self._decode(r) for r in rows]
        needle = str(q or "").strip().casefold()
        if needle:
            out = [r for r in out if needle in r["title"].casefold() or needle in r["text"].casefold()]
        return out

    def revisions(self, project_id: str, key: str) -> List[Dict[str, Any]]:
        req_id = f"{str(project_id or '').strip()}:{str(key or '').strip().upper()}"
        if not self.path.exists():
            return []
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM requirement_revisions WHERE req_id=? ORDER BY revision", (req_id,)
            ).fetchall()
        return [self._decode_revision(r) for r in rows]

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    _PATCHABLE = ("title", "text", "source", "acceptance", "status")

    def update(
        self, project_id: str, key: str, patch: Mapping[str, Any], *,
        by: str = "human", actor: str = "", change_note: str = "",
    ) -> Dict[str, Any]:
        unknown = [k for k in patch if k not in self._PATCHABLE]
        if unknown:
            raise RequirementsError(f"not a patchable field: {', '.join(sorted(unknown))}")
        by = str(by or "human").strip().lower()
        if by not in PROPOSED_BY:
            raise RequirementsError(f"invalid by: {by!r} (one of {PROPOSED_BY})")
        new_status = None
        if "status" in patch:
            new_status = str(patch["status"] or "").strip().lower()
            if new_status not in STATUSES:
                raise RequirementsError(f"invalid status: {new_status!r} (one of {STATUSES})")
            if by == "model" and new_status in _HUMAN_ONLY_STATUSES:
                raise RequirementsError(
                    f"a model cannot set status to {new_status!r} -- only a human accepts or rejects "
                    "a requirement", "requirements.model_cannot_decide",
                )
        req_id = f"{str(project_id or '').strip()}:{str(key or '').strip().upper()}"
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM requirements WHERE req_id=?", (req_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"{key} not found in project {project_id}")
            title = str(patch.get("title", row["title"]) or row["title"]).strip() or row["title"]
            text = str(patch.get("text", row["text"]) if "text" in patch else row["text"])
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise RequirementsError(f"text exceeds {MAX_TEXT_BYTES} bytes", "requirements.too_large")
            source = row["source"]
            if "source" in patch:
                source = str(patch["source"] or "").strip().lower()
                if source not in SOURCES:
                    raise RequirementsError(f"invalid source: {source!r} (one of {SOURCES})")
            acc = json.loads(row["acceptance_json"] or "[]")
            if "acceptance" in patch:
                acc = _acceptance(patch["acceptance"])
            status = new_status if new_status is not None else row["status"]

            changed = (
                title != row["title"] or text != row["text"] or source != row["source"]
                or acc != json.loads(row["acceptance_json"] or "[]") or status != row["status"]
            )
            if not changed:
                return self.get_or_raise(project_id, key)

            next_revision = int(row["current_revision"]) + 1
            stamp = _now()
            db.execute(
                "UPDATE requirements SET title=?, text=?, source=?, acceptance_json=?, status=?, "
                "current_revision=?, updated_at=? WHERE req_id=?",
                (title, text, source, _json(acc), status, next_revision, stamp, req_id),
            )
            # Immutable append -- the row just replaced above is never the
            # historical record; `requirement_revisions` is (ADP-18 "el
            # pasado no se edita").
            db.execute(
                "INSERT INTO requirement_revisions (id, req_id, revision, title, text, source, "
                "acceptance_json, status, changed_by, change_note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"rev_{uuid.uuid4().hex[:16]}", req_id, next_revision, title, text, source,
                 _json(acc), status, str(actor or by), str(change_note or ""), stamp),
            )
        return self.get_or_raise(project_id, key)

    # ------------------------------------------------------------------
    # Links (evidence.py reads/writes these; kept here since they are just
    # rows in the same database, same split project_board.py uses for its
    # own links/refs tables)
    # ------------------------------------------------------------------
    def add_link(
        self, project_id: str, key: str, *, kind: str, target: str, revision: str = "",
        content_hash: Optional[str] = None, state: str = "linked", created_by: str = "",
    ) -> Dict[str, Any]:
        kind = str(kind or "").strip().lower()
        target = str(target or "").strip()
        if kind not in LINK_KINDS:
            raise RequirementsError(f"unknown link kind: {kind!r} (one of {LINK_KINDS})", "requirements.invalid_link")
        if not target:
            raise RequirementsError("target is required", "requirements.invalid_link")
        state = str(state or "linked").strip().lower()
        if state not in LINK_STATES:
            state = "linked"
        req_id = f"{str(project_id or '').strip()}:{str(key or '').strip().upper()}"
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM requirements WHERE req_id=?", (req_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"{key} not found in project {project_id}")
            link_id = f"lnk_{uuid.uuid4().hex[:16]}"
            stamp = _now()
            db.execute(
                "INSERT INTO requirement_links (id, req_id, kind, target, revision, "
                "req_revision_at_link, content_hash, state, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (link_id, req_id, kind, target, str(revision or ""), int(row["current_revision"]),
                 content_hash, state, str(created_by or ""), stamp, stamp),
            )
            link_row = db.execute("SELECT * FROM requirement_links WHERE id=?", (link_id,)).fetchone()
        return self._decode_link(link_row)

    def list_links(self, project_id: str, key: str) -> List[Dict[str, Any]]:
        req_id = f"{str(project_id or '').strip()}:{str(key or '').strip().upper()}"
        if not self.path.exists():
            return []
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM requirement_links WHERE req_id=? ORDER BY created_at", (req_id,)
            ).fetchall()
        return [self._decode_link(r) for r in rows]

    def set_link_state(self, link_id: str, state: str) -> None:
        state = str(state or "").strip().lower()
        if state not in LINK_STATES:
            raise RequirementsError(f"invalid link state: {state!r} (one of {LINK_STATES})")
        with self._db(write=True) as db:
            db.execute(
                "UPDATE requirement_links SET state=?, updated_at=? WHERE id=?",
                (state, _now(), link_id),
            )

    def all_requirement_keys(self, project_id: str) -> List[str]:
        """Every ``REQ-N`` key this project has -- used by ``evidence.matrix``
        to build the whole-project coverage table without the caller having
        to enumerate keys itself."""
        if not self.path.exists():
            return []
        with self._db() as db:
            rows = db.execute(
                "SELECT key FROM requirements WHERE project_id=? ORDER BY key", (project_id,)
            ).fetchall()
        return [r["key"] for r in rows]

    def read_sidecar(self, workspace: str) -> Dict[str, Any]:
        """Parse ``<workspace>/.faustus/requirements.yaml`` if it exists.
        Read-only projection -- items here are NOT written to
        ``requirements.sqlite3``; a hand-authored sidecar is a human's own
        record, not automatically a database decision (ADP-18 limit: "no
        modificar código solo para llenarlo de anotaciones" applies just as
        much to auto-importing a file nobody asked to import). Returns
        ``{"items": [...], "errors": [...], "path": ""}`` -- an empty/absent
        sidecar is not an error, just zero items."""
        workspace = str(workspace or "").strip()
        if not workspace:
            return {"items": [], "errors": [], "path": ""}
        base = os.path.realpath(workspace)
        sidecar_path = os.path.join(base, ".faustus", "requirements.yaml")
        real = os.path.realpath(sidecar_path)
        try:
            common = os.path.commonpath([base, real])
        except ValueError:
            common = ""
        if common != base or not os.path.isfile(real):
            return {"items": [], "errors": [], "path": ""}
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            return {"items": [], "errors": [f"could not read sidecar: {exc}"], "path": sidecar_path}
        parsed = parse_sidecar(text)
        parsed["path"] = sidecar_path
        return parsed


# ---------------------------------------------------------------------------
# Module-level convenience over the default store (mirrors project_board.py)
# ---------------------------------------------------------------------------
def create(project_id: str, **kwargs: Any) -> Dict[str, Any]:
    return Store().create(project_id, **kwargs)


def get(project_id: str, key: str) -> Optional[Dict[str, Any]]:
    return Store().get(project_id, key)


def get_or_raise(project_id: str, key: str) -> Dict[str, Any]:
    return Store().get_or_raise(project_id, key)


def list_requirements(project_id: str, **kwargs: Any) -> List[Dict[str, Any]]:
    return Store().list(project_id, **kwargs)


def revisions(project_id: str, key: str) -> List[Dict[str, Any]]:
    return Store().revisions(project_id, key)


def update(project_id: str, key: str, patch: Mapping[str, Any], **kwargs: Any) -> Dict[str, Any]:
    return Store().update(project_id, key, patch, **kwargs)


def add_link(project_id: str, key: str, **kwargs: Any) -> Dict[str, Any]:
    return Store().add_link(project_id, key, **kwargs)


def list_links(project_id: str, key: str) -> List[Dict[str, Any]]:
    return Store().list_links(project_id, key)


def all_requirement_keys(project_id: str) -> List[str]:
    return Store().all_requirement_keys(project_id)


def read_sidecar(workspace: str) -> Dict[str, Any]:
    return Store().read_sidecar(workspace)

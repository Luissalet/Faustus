"""document_links.py — wiki-links and backlinks between Documents (ADP-06, W1-E).

Faustus documents (`core/database.py::Document`) live in the same owner-scoped
table the Studio's Library already uses; this module adds `[[Title]]` /
`[[doc:<id>]]` / `[[Title|alias]]` cross-references between them without
inventing a second document store — it only *projects* what `parse_links`
finds in `Document.current_content` into a small, reconstructible index.

Design notes (read before changing resolution behaviour):

- **Identity first.** `[[doc:<id>]]` resolves by the document's stable id and
  survives a title rename by construction — the id never changes.
  `[[Title]]` resolves by an exact (case-insensitive) title match at REBUILD
  time; renaming a document breaks title-based links that pointed at its old
  title (expected — a title is not a stable identity) but never at its id.
- **Never guess.** Zero title matches -> `broken`. More than one match with
  the same title -> `ambiguous` with every candidate id listed; resolution
  never picks "the first one". A ref to another owner's document is
  indistinguishable from a ref to nothing — resolution is always scoped to
  the source document's own `owner`, so it can never leak cross-owner
  existence (same posture as `_verify_doc_owner`/`_owner_session_filter` in
  `routes/document/document_helpers.py`).
- **Reconstructible.** The sqlite index is a cache of what `parse_links` +
  `resolve` produce; deleting `document_links.sqlite3` and calling
  `rebuild_all_for_owner` for every owner reproduces it exactly. Nothing here
  is a second source of truth for document content or identity.
- **No traversal.** A link target containing a `..` path segment (an attempt
  to walk outside a workspace-relative reference) is never resolved and is
  recorded as `rejected`, distinct from `broken` (not found) or `ambiguous`
  (found more than once) — it is refused outright, the same posture
  `routes/workspace_routes.py` takes for `..` in a file path.

Self-contained SQLite (own file under `DATA_DIR`, own schema, own writer
transaction per call) — the same shape as `src/project_board.py` and
`src/question_store.py`, not a table bolted onto `core/database.py`: a
corrupt or deleted `document_links.sqlite3` loses a navigation convenience,
never a document.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

VERSION = 1

#: `[[Title]]`, `[[doc:<id>]]`, `[[Title|alias]]`. Deliberately excludes `[`
#: and `]` from the target/alias groups so nested brackets never confuse the
#: match, and the alias is optional (single `|`-split).
_LINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]|]+))?\]\]")

STATUS_RESOLVED = "resolved"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_BROKEN = "broken"
STATUS_REJECTED = "rejected"


@dataclass
class LinkToken:
    raw: str                 # the full "Title" or "doc:<id>" target text (no brackets, no alias)
    target_type: str         # "id" | "title"
    target_value: str
    alias: Optional[str]
    start: int
    end: int


def parse_links(text: str) -> List[LinkToken]:
    """Extract every `[[...]]` token from `text`, in document order.

    Pure and total: never raises on malformed input (an unmatched `[[` with
    no closing `]]` simply produces no token for that span), so a caller
    never needs to sanitize content before parsing it.
    """
    out: List[LinkToken] = []
    if not text:
        return out
    for m in _LINK_RE.finditer(text):
        raw = m.group(1).strip()
        alias = (m.group(2) or "").strip() or None
        if not raw:
            continue
        if raw.lower().startswith("doc:"):
            target_type = "id"
            target_value = raw[4:].strip()
        else:
            target_type = "title"
            target_value = raw
        if not target_value:
            continue
        out.append(LinkToken(raw=raw, target_type=target_type, target_value=target_value,
                              alias=alias, start=m.start(), end=m.end()))
    return out


def _is_path_traversal(value: str) -> bool:
    """A target that tries to walk outside a workspace-relative reference.

    Document titles/ids never legitimately contain a `..` path segment, so
    this is a narrow, false-positive-free check — not a general path
    validator (documents have no filesystem path at all)."""
    parts = re.split(r"[\\/]", value)
    return any(p == ".." for p in parts)


@dataclass
class ResolvedLink:
    token: LinkToken
    status: str                         # resolved | ambiguous | broken | rejected
    doc_id: Optional[str] = None
    candidates: List[str] = field(default_factory=list)


def resolve(sa_db, owner: Optional[str], token: LinkToken) -> ResolvedLink:
    """Resolve one `LinkToken` against `owner`'s own documents.

    `sa_db` is a SQLAlchemy session (the caller's, so this runs inside
    whatever transaction is already open — no new engine/connection here).
    Every query below filters by `Document.owner == owner`: a link can never
    resolve into, or reveal the existence of, another owner's document.
    """
    from core.database import Document  # local: avoid a module-load cycle with core.database

    if _is_path_traversal(token.target_value):
        return ResolvedLink(token=token, status=STATUS_REJECTED)

    if token.target_type == "id":
        row = (sa_db.query(Document.id)
               .filter(Document.id == token.target_value, Document.owner == owner)
               .first())
        if row:
            return ResolvedLink(token=token, status=STATUS_RESOLVED, doc_id=row[0])
        return ResolvedLink(token=token, status=STATUS_BROKEN)

    # title match: exact, case-insensitive, scoped to this owner only.
    rows = (sa_db.query(Document.id)
            .filter(Document.owner == owner, func_lower(Document.title) == token.target_value.lower())
            .all())
    ids = [r[0] for r in rows]
    if not ids:
        return ResolvedLink(token=token, status=STATUS_BROKEN)
    if len(ids) > 1:
        return ResolvedLink(token=token, status=STATUS_AMBIGUOUS, candidates=ids)
    return ResolvedLink(token=token, status=STATUS_RESOLVED, doc_id=ids[0])


def func_lower(col):
    from sqlalchemy import func
    return func.lower(col)


# ---------------------------------------------------------------------------
# SQLite index — same shape as src/project_board.py's Store
# ---------------------------------------------------------------------------

def default_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "document_links.sqlite3"


class LinkStore:
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
                    raise ValueError("unrecognized document_links database")
                db.executescript(
                    """
                    CREATE TABLE links (
                        id TEXT PRIMARY KEY,
                        source_doc_id TEXT NOT NULL,
                        owner TEXT NOT NULL DEFAULT '',
                        raw TEXT NOT NULL,
                        target_type TEXT NOT NULL,
                        target_value TEXT NOT NULL,
                        alias TEXT,
                        status TEXT NOT NULL,
                        resolved_doc_id TEXT,
                        candidates_json TEXT NOT NULL DEFAULT '[]',
                        position INTEGER NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX links_source ON links(source_doc_id);
                    CREATE INDEX links_resolved ON links(resolved_doc_id);
                    CREATE INDEX links_owner ON links(owner);
                    """
                )
                db.execute(f"PRAGMA user_version={VERSION}")
            elif version != VERSION:
                raise ValueError("unsupported document_links database version")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        import json
        return {
            "id": row["id"],
            "source_doc_id": row["source_doc_id"],
            "raw": row["raw"],
            "target_type": row["target_type"],
            "target_value": row["target_value"],
            "alias": row["alias"],
            "status": row["status"],
            "resolved_doc_id": row["resolved_doc_id"],
            "candidates": json.loads(row["candidates_json"] or "[]"),
            "position": row["position"],
            "updated_at": row["updated_at"],
        }

    def replace_for_document(self, source_doc_id: str, owner: Optional[str],
                              resolved: Sequence[ResolvedLink]) -> None:
        """Atomically swap `source_doc_id`'s outgoing links for `resolved`.

        Delete-then-insert inside one write transaction: a reader never sees
        a half-updated set (either the old links or the new ones, never a
        mix), and rebuilding is always a full recompute — never a diff — so
        it is safe to call after every save with no drift risk."""
        import json
        from src.contracts.base import now_iso
        now = now_iso()
        with self._db(write=True) as db:
            db.execute("DELETE FROM links WHERE source_doc_id=?", (source_doc_id,))
            for i, r in enumerate(resolved):
                db.execute(
                    "INSERT INTO links (id, source_doc_id, owner, raw, target_type, target_value, "
                    "alias, status, resolved_doc_id, candidates_json, position, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), source_doc_id, owner or "", r.token.raw,
                     r.token.target_type, r.token.target_value, r.token.alias, r.status,
                     r.doc_id, json.dumps(r.candidates), i, now),
                )

    def links_for(self, source_doc_id: str) -> List[Dict[str, Any]]:
        with self._db(write=False) as db:
            rows = db.execute(
                "SELECT * FROM links WHERE source_doc_id=? ORDER BY position", (source_doc_id,)
            ).fetchall()
            return [self._decode(r) for r in rows]

    def backlinks_for(self, doc_id: str, owner: Optional[str]) -> List[Dict[str, Any]]:
        """Every RESOLVED link that points at `doc_id`, scoped to `owner`.

        `owner` here is the requesting user (already proven to own `doc_id`
        by the caller's `_verify_doc_owner`); every stored row's `owner` is
        the SOURCE document's owner, which `resolve()` only ever sets to the
        same owner it searched within — so this can never surface another
        owner's document referencing this one."""
        with self._db(write=False) as db:
            rows = db.execute(
                "SELECT * FROM links WHERE resolved_doc_id=? AND owner=? AND status=? "
                "ORDER BY updated_at",
                (doc_id, owner or "", STATUS_RESOLVED),
            ).fetchall()
            return [self._decode(r) for r in rows]

    def clear_for_document(self, source_doc_id: str) -> None:
        with self._db(write=True) as db:
            db.execute("DELETE FROM links WHERE source_doc_id=?", (source_doc_id,))


_store: Optional[LinkStore] = None


def get_store() -> LinkStore:
    global _store
    if _store is None:
        _store = LinkStore()
    return _store


# ---------------------------------------------------------------------------
# High-level entry points used by routes/document_links_routes.py and the
# document save hook in routes/document/document_routes.py.
# ---------------------------------------------------------------------------

def rebuild_for_document(sa_db, doc, *, store: Optional[LinkStore] = None) -> List[Dict[str, Any]]:
    """Recompute and persist `doc`'s outgoing links from its current content.

    Called from the document save path (`update_document`) and from the
    manual `/rebuild` route — the same function either way, so the index
    never drifts between "updated on save" and "rebuilt on demand"."""
    store = store or get_store()
    tokens = parse_links(doc.current_content or "")
    resolved = [resolve(sa_db, doc.owner, t) for t in tokens]
    store.replace_for_document(doc.id, doc.owner, resolved)
    return [_link_result_dict(r) for r in resolved]


def rebuild_all_for_owner(sa_db, owner: Optional[str], *, store: Optional[LinkStore] = None) -> int:
    """Rebuild the whole index for every document `owner` has. Returns the
    number of documents processed. Used by `POST /api/documents/links/rebuild`
    for recovery after a deleted/corrupt index file — the index is defined
    to be reconstructible from `Document.current_content` alone."""
    from core.database import Document
    store = store or get_store()
    docs = sa_db.query(Document).filter(Document.owner == owner).all()
    for doc in docs:
        rebuild_for_document(sa_db, doc, store=store)
    return len(docs)


def _link_result_dict(r: ResolvedLink) -> Dict[str, Any]:
    return {
        "raw": r.token.raw,
        "target_type": r.token.target_type,
        "target_value": r.token.target_value,
        "alias": r.token.alias,
        "status": r.status,
        "doc_id": r.doc_id,
        "candidates": r.candidates,
    }


def get_links(doc_id: str, *, store: Optional[LinkStore] = None) -> List[Dict[str, Any]]:
    return (store or get_store()).links_for(doc_id)


def get_backlinks(doc_id: str, owner: Optional[str], *, store: Optional[LinkStore] = None) -> List[Dict[str, Any]]:
    return (store or get_store()).backlinks_for(doc_id, owner)

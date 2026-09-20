"""src/project_concepts.py — a persistent, per-project graph of architecture
concepts written by the agent itself.

Faustus already indexes code mechanically (``src/code_graph/``,
``src/context_engine/code_index.py``) and tracks requirements/decisions
(``src/requirements/``, ``src/project_board.py``), joined for one file or
requirement by ``src/knowledge_neighborhood.py``. None of that captures the
agent's own understanding of *why* a subsystem exists, what it is called,
and how it relates to the rest of the project in the agent's own words —
the thing a human engineer would put in an architecture doc, except this one
is written by the model as it works and stays queryable across sessions.

The model is the indexer. There is no AST pass, no static analysis: an
agent reads code, decides "this is a concept worth remembering", and calls
``concept_upsert`` with a short slug, a kind, a summary, and the files/
symbols it grounds itself in (``refs``). Later — in this session or a new
one — ``concepts_understand(query)`` does a semantic search over those
concepts (cosine, brute force, via ``src.embeddings`` — same local
fastembed fallback the rest of the app uses, so this works fully offline)
and returns the closest concepts plus their one-hop neighbours, so the
agent (or a person) re-learns in one call what took a full exploration the
first time.

Storage: one SQLite file per project under
``DATA_DIR/project_concepts/<project_key>.db`` (WAL mode, like
``src.project_board``/``src.requirements.store``). ``project_key`` prefers
the real project id (``proj-<id>``); a chat with no project bound falls
back to a hash of the normalized workspace path (``ws-<hash>``, the same
shape ``src.project_audit.workspace_key`` already uses for its own
per-workspace fallback) so concepts a solo workspace user writes are not
silently dropped.

Three tables:

  ``concepts``  the node: id (slug), name, kind (one of ``KINDS``), summary,
                details, refs (JSON list of ``"path"`` or ``"path@symbol"``
                strings), parent_id (for a simple containment tree),
                created/updated timestamps, ``removed_at`` (soft delete —
                a concept is never hard-deleted, so history stays whole),
                embedding (BLOB, float32).
  ``edges``     a typed relation between two concepts: ``connects_to``,
                ``depends_on``, ``implements``, ``calls``, ``configured_by``
                (``RELATIONS``) plus an optional free-text note.
  ``history``   append-only log of every create/update/remove/link/unlink,
                with the before/after JSON so "why does the graph say this"
                has an answer.

Staleness: a concept's ``refs`` are checked the same way
``src.doc_claims`` grounds a doc's backticked paths/symbols — a path that no
longer exists under the workspace, or a ``path@symbol`` whose symbol is no
longer defined there, marks the concept ``stale``. This is deliberately the
*only* notion of staleness here: no attempt to detect "this summary is now
wrong", only "this citation no longer resolves" — the same discipline
``doc_claims`` documents for its own broken/stale split.

This module never touches ``src.memory`` / ``memory_engine`` (personal,
cross-project memory) or ``src.project_board`` (issues) — a concept is
neither.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

VERSION = 1

KINDS: Tuple[str, ...] = ("feature", "module", "pattern", "config", "decision", "component")
RELATIONS: Tuple[str, ...] = ("connects_to", "depends_on", "implements", "calls", "configured_by")
ACTIONS: Tuple[str, ...] = ("create", "update", "remove", "link", "unlink")

MAX_TEXT_BYTES = 200_000
MAX_REFS = 64
MAX_UNDERSTAND_K = 25
DEFAULT_UNDERSTAND_K = 6


class ProjectConceptsError(ValueError):
    def __init__(self, message: str, error_class: str = "project_concepts.error"):
        super().__init__(message)
        self.error_class = error_class


# ---------------------------------------------------------------------------
# Project key resolution
# ---------------------------------------------------------------------------
def _safe_key(key: str) -> str:
    return "".join(ch for ch in str(key or "") if ch.isalnum() or ch in "-_")[:80] or "unknown"


def workspace_project_key(workspace: str) -> str:
    """Same shape as ``src.project_audit.workspace_key``: a normalized,
    case-folded-on-Windows realpath, hashed — the fallback identity for a
    workspace with no project bound."""
    key = os.path.realpath(os.path.expanduser(str(workspace or ""))).replace("\\", "/")
    if os.name == "nt":
        key = key.lower()
    return "ws-" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


def resolve_project_key(*, project_id: Optional[str] = None, workspace: Optional[str] = None) -> str:
    project_id = (project_id or "").strip()
    if project_id:
        return "proj-" + _safe_key(project_id)
    workspace = (workspace or "").strip()
    if workspace:
        return workspace_project_key(workspace)
    raise ProjectConceptsError(
        "resolve_project_key: need a project_id or a workspace",
        "project_concepts.no_project",
    )


def _slugify(name: str, existing: Optional[set] = None) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    s = s[:64] or uuid.uuid4().hex[:12]
    if not existing:
        return s
    base, i, out = s, 2, s
    while out in existing:
        out = f"{base}-{i}"
        i += 1
    return out


def _data_dir() -> str:
    from src.constants import DATA_DIR
    d = os.path.join(DATA_DIR, "project_concepts")
    os.makedirs(d, exist_ok=True)
    return d


def db_path(project_key: str) -> str:
    return os.path.join(_data_dir(), _safe_key(project_key) + ".db")


def _now() -> str:
    from src.contracts.base import now_iso
    return now_iso()


def _json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProjectConceptsError(f"value is not JSON-serializable: {exc}", "project_concepts.bad_value") from exc
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ProjectConceptsError("value exceeds size limit", "project_concepts.too_large")
    return text


def _loads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Embeddings (src.embeddings — HTTP endpoint if configured, else local
# fastembed; either way the same client every other vector feature uses, so
# this works fully offline).
# ---------------------------------------------------------------------------
def _embed_texts(texts: Sequence[str]):
    import numpy as np
    from src.embeddings import get_embedding_client
    texts = [str(t or "")[:4000] for t in texts]
    if not any(texts):
        return np.zeros((len(texts), 1), dtype="float32")
    try:
        client = get_embedding_client()
        vecs = client.encode(texts, normalize_embeddings=True)
        return np.asarray(vecs, dtype="float32")
    except Exception:
        logger.warning("project_concepts: embedding failed, falling back to zero vectors", exc_info=True)
        return np.zeros((len(texts), 1), dtype="float32")


def _embed_one(text: str):
    return _embed_texts([text])[0]


def _to_blob(vec) -> bytes:
    import numpy as np
    return np.asarray(vec, dtype="float32").tobytes()


def _from_blob(blob: Optional[bytes]):
    import numpy as np
    if not blob:
        return None
    return np.frombuffer(blob, dtype="float32")


def _cosine(a, b) -> float:
    import numpy as np
    if a is None or b is None or a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _embed_text_for_concept(name: str, kind: str, summary: str, details: str) -> str:
    return f"{name} ({kind}): {summary}\n{details}".strip()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class Store:
    """Stateless-safe wrapper: cheap to build fresh per call, like
    ``project_board.Store``/``requirements.store.Store``."""

    def __init__(self, project_key: str, path: Optional[str] = None):
        if not project_key:
            raise ProjectConceptsError("project_key is required", "project_concepts.no_project")
        self.project_key = project_key
        self.path = path or db_path(project_key)
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self._conn() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS concepts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '',
                    refs TEXT NOT NULL DEFAULT '[]',
                    parent_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    removed_at TEXT,
                    embedding BLOB
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS edges (
                    id TEXT PRIMARY KEY,
                    src TEXT NOT NULL,
                    dst TEXT NOT NULL,
                    rel TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    removed_at TEXT
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id TEXT PRIMARY KEY,
                    concept_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    action TEXT NOT NULL,
                    before TEXT,
                    after TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS concepts_parent ON concepts(parent_id)")
            db.execute("CREATE INDEX IF NOT EXISTS edges_src ON edges(src)")
            db.execute("CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst)")
            db.execute("CREATE INDEX IF NOT EXISTS history_concept ON history(concept_id, ts)")
            db.commit()

    # -- internal reads --------------------------------------------------
    def _row_to_concept(self, row: sqlite3.Row, *, with_embedding: bool = False) -> Dict[str, Any]:
        out = {
            "id": row["id"], "name": row["name"], "kind": row["kind"],
            "summary": row["summary"], "details": row["details"],
            "refs": _loads(row["refs"], []),
            "parent_id": row["parent_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "removed_at": row["removed_at"],
            "project_key": self.project_key,
        }
        if with_embedding:
            out["_embedding"] = _from_blob(row["embedding"])
        return out

    def _get_row(self, db: sqlite3.Connection, concept_id: str) -> Optional[sqlite3.Row]:
        return db.execute("SELECT * FROM concepts WHERE id = ?", (concept_id,)).fetchone()

    def _existing_ids(self, db: sqlite3.Connection) -> set:
        return {r["id"] for r in db.execute("SELECT id FROM concepts")}

    def _log(self, db: sqlite3.Connection, concept_id: str, action: str,
              before: Any = None, after: Any = None) -> None:
        if action not in ACTIONS:
            raise ProjectConceptsError(f"unknown history action: {action}", "project_concepts.bad_action")
        db.execute(
            "INSERT INTO history (id, concept_id, ts, action, before, after) VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, concept_id, _now(), action,
             json.dumps(before, ensure_ascii=False) if before is not None else None,
             json.dumps(after, ensure_ascii=False) if after is not None else None),
        )

    # -- public API --------------------------------------------------------
    def upsert_concept(
        self, *, name: str, kind: str, summary: str = "", details: str = "",
        refs: Optional[Sequence[str]] = None, parent_id: Optional[str] = None,
        concept_id: Optional[str] = None, embed: bool = True,
    ) -> Dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ProjectConceptsError("name is required", "project_concepts.bad_input")
        kind = str(kind or "").strip().lower()
        if kind not in KINDS:
            raise ProjectConceptsError(f"kind must be one of {KINDS}", "project_concepts.bad_kind")
        summary = str(summary or "").strip()
        details = str(details or "").strip()
        refs_list = [str(r).strip() for r in (refs or []) if str(r).strip()][:MAX_REFS]
        refs_json = _json(refs_list)
        _json(details)  # size guard

        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if parent_id is not None and self._get_row(db, parent_id) is None:
                    raise ProjectConceptsError(f"parent_id {parent_id!r} not found", "project_concepts.not_found")
                existing_ids = self._existing_ids(db)
                is_update = bool(concept_id) and concept_id in existing_ids
                cid = concept_id if is_update else (concept_id or _slugify(name, existing_ids))
                now = _now()
                embedding_blob = None
                if embed:
                    vec = _embed_one(_embed_text_for_concept(name, kind, summary, details))
                    embedding_blob = _to_blob(vec)

                if is_update:
                    before_row = self._get_row(db, cid)
                    before = self._row_to_concept(before_row)
                    db.execute(
                        "UPDATE concepts SET name=?, kind=?, summary=?, details=?, refs=?, "
                        "parent_id=?, updated_at=?, removed_at=NULL"
                        + (", embedding=?" if embed else "") + " WHERE id=?",
                        (name, kind, summary, details, refs_json, parent_id, now)
                        + ((embedding_blob,) if embed else ()) + (cid,),
                    )
                    after = self._row_to_concept(self._get_row(db, cid))
                    self._log(db, cid, "update", before=before, after=after)
                else:
                    db.execute(
                        "INSERT INTO concepts (id, name, kind, summary, details, refs, parent_id, "
                        "created_at, updated_at, removed_at, embedding) VALUES (?,?,?,?,?,?,?,?,?,NULL,?)",
                        (cid, name, kind, summary, details, refs_json, parent_id, now, now, embedding_blob),
                    )
                    after = self._row_to_concept(self._get_row(db, cid))
                    self._log(db, cid, "create", before=None, after=after)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return after

    def get_concept(self, concept_id: str, *, include_removed: bool = False) -> Optional[Dict[str, Any]]:
        with self._conn() as db:
            row = self._get_row(db, concept_id)
            if row is None:
                return None
            if row["removed_at"] and not include_removed:
                return None
            out = self._row_to_concept(row)
            out["incoming"] = [
                dict(r) for r in db.execute(
                    "SELECT * FROM edges WHERE dst=? AND removed_at IS NULL ORDER BY created_at", (concept_id,)
                )
            ]
            out["outgoing"] = [
                dict(r) for r in db.execute(
                    "SELECT * FROM edges WHERE src=? AND removed_at IS NULL ORDER BY created_at", (concept_id,)
                )
            ]
            out["children"] = [
                self._row_to_concept(r) for r in db.execute(
                    "SELECT * FROM concepts WHERE parent_id=? AND removed_at IS NULL ORDER BY name", (concept_id,)
                )
            ]
            return out

    def link(self, src: str, dst: str, rel: str, note: str = "") -> Dict[str, Any]:
        rel = str(rel or "").strip().lower()
        if rel not in RELATIONS:
            raise ProjectConceptsError(f"rel must be one of {RELATIONS}", "project_concepts.bad_rel")
        with self._conn() as db:
            if self._get_row(db, src) is None:
                raise ProjectConceptsError(f"src concept {src!r} not found", "project_concepts.not_found")
            if self._get_row(db, dst) is None:
                raise ProjectConceptsError(f"dst concept {dst!r} not found", "project_concepts.not_found")
            row = db.execute(
                "SELECT id FROM edges WHERE src=? AND dst=? AND rel=? AND removed_at IS NULL", (src, dst, rel)
            ).fetchone()
            if row is not None:
                edge = {"id": row["id"], "src": src, "dst": dst, "rel": rel, "note": note}
                return edge
            eid = uuid.uuid4().hex
            now = _now()
            db.execute(
                "INSERT INTO edges (id, src, dst, rel, note, created_at) VALUES (?,?,?,?,?,?)",
                (eid, src, dst, rel, str(note or ""), now),
            )
            self._log(db, src, "link", after={"id": eid, "src": src, "dst": dst, "rel": rel, "note": note})
            db.commit()
            return {"id": eid, "src": src, "dst": dst, "rel": rel, "note": note, "created_at": now}

    def unlink(self, src: str, dst: str, rel: Optional[str] = None) -> int:
        with self._conn() as db:
            now = _now()
            if rel:
                rows = db.execute(
                    "SELECT id FROM edges WHERE src=? AND dst=? AND rel=? AND removed_at IS NULL", (src, dst, rel)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id FROM edges WHERE src=? AND dst=? AND removed_at IS NULL", (src, dst)
                ).fetchall()
            for r in rows:
                db.execute("UPDATE edges SET removed_at=? WHERE id=?", (now, r["id"]))
                self._log(db, src, "unlink", before={"id": r["id"], "src": src, "dst": dst, "rel": rel})
            db.commit()
            return len(rows)

    def remove_concept(self, concept_id: str) -> bool:
        with self._conn() as db:
            row = self._get_row(db, concept_id)
            if row is None or row["removed_at"]:
                return False
            before = self._row_to_concept(row)
            now = _now()
            db.execute("UPDATE concepts SET removed_at=? WHERE id=?", (now, concept_id))
            db.execute(
                "UPDATE edges SET removed_at=? WHERE (src=? OR dst=?) AND removed_at IS NULL",
                (now, concept_id, concept_id),
            )
            self._log(db, concept_id, "remove", before=before, after=None)
            db.commit()
            return True

    def list_roots(self) -> List[Dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM concepts WHERE parent_id IS NULL AND removed_at IS NULL ORDER BY name"
            ).fetchall()
            out = []
            for row in rows:
                c = self._row_to_concept(row)
                c["child_count"] = db.execute(
                    "SELECT COUNT(*) c FROM concepts WHERE parent_id=? AND removed_at IS NULL", (row["id"],)
                ).fetchone()["c"]
                out.append(c)
            return out

    def all_concepts(self) -> List[Dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM concepts WHERE removed_at IS NULL ORDER BY name").fetchall()
            return [self._row_to_concept(r) for r in rows]

    def graph(self) -> Dict[str, Any]:
        with self._conn() as db:
            nodes = [self._row_to_concept(r) for r in db.execute(
                "SELECT * FROM concepts WHERE removed_at IS NULL"
            )]
            edges = [dict(r) for r in db.execute(
                "SELECT id, src, dst, rel, note FROM edges WHERE removed_at IS NULL"
            )]
        degree: Dict[str, int] = {}
        for e in edges:
            degree[e["src"]] = degree.get(e["src"], 0) + 1
            degree[e["dst"]] = degree.get(e["dst"], 0) + 1
        for n in nodes:
            n["degree"] = degree.get(n["id"], 0)
        return {"nodes": nodes, "edges": edges}

    def understand(self, query: str, k: int = DEFAULT_UNDERSTAND_K) -> Dict[str, Any]:
        k = max(1, min(int(k or DEFAULT_UNDERSTAND_K), MAX_UNDERSTAND_K))
        query = str(query or "").strip()
        if not query:
            return {"query": query, "concepts": [], "neighbors": []}
        qvec = _embed_one(query)
        with self._conn() as db:
            rows = db.execute("SELECT * FROM concepts WHERE removed_at IS NULL").fetchall()
        scored = []
        for row in rows:
            c = self._row_to_concept(row, with_embedding=True)
            vec = c.pop("_embedding")
            score = _cosine(qvec, vec) if vec is not None else 0.0
            scored.append((score, c))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = scored[:k]
        top_ids = {c["id"] for _, c in top}
        neighbors: Dict[str, Dict[str, Any]] = {}
        if top_ids:
            with self._conn() as db:
                placeholders = ",".join("?" * len(top_ids))
                for r in db.execute(
                    f"SELECT * FROM edges WHERE removed_at IS NULL AND "
                    f"(src IN ({placeholders}) OR dst IN ({placeholders}))",
                    tuple(top_ids) * 2,
                ):
                    other = r["dst"] if r["src"] in top_ids else r["src"]
                    if other in top_ids or other in neighbors:
                        continue
                    orow = self._get_row(db, other)
                    if orow is not None and not orow["removed_at"]:
                        neighbors[other] = self._row_to_concept(orow)
        return {
            "query": query,
            "concepts": [{"score": round(score, 4), **c} for score, c in top if score > 0 or len(rows) <= k],
            "neighbors": list(neighbors.values()),
        }

    def history(self, concept_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT id, concept_id, ts, action, before, after FROM history "
                "WHERE concept_id=? ORDER BY ts DESC LIMIT ?",
                (concept_id, max(1, min(int(limit or 100), 500))),
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "concept_id": r["concept_id"], "ts": r["ts"], "action": r["action"],
                "before": _loads(r["before"], None), "after": _loads(r["after"], None),
            })
        return out

    def stale_check(self, concept: Mapping[str, Any], workspace: str) -> Dict[str, Any]:
        """Ground each ref ("path" or "path@symbol") against `workspace`,
        the same discipline `src.doc_claims` uses for a doc's backticked
        claims: a missing path, or a symbol no longer defined in an
        existing path, marks the ref (and the concept) stale."""
        refs = concept.get("refs") or []
        workspace = str(workspace or "").strip()
        issues: List[Dict[str, str]] = []
        if not workspace:
            return {"stale": False, "checked": False, "issues": [], "reason": "no workspace to check against"}
        root = os.path.realpath(os.path.expanduser(workspace))
        for ref in refs:
            ref = str(ref)
            path_part, _, symbol = ref.partition("@")
            path_part = path_part.strip()
            if not path_part:
                continue
            full = os.path.realpath(os.path.join(root, path_part))
            if os.path.commonpath([root, full]) != root:
                issues.append({"ref": ref, "why": "path_outside_workspace"})
                continue
            if not os.path.exists(full):
                issues.append({"ref": ref, "why": "path_missing"})
                continue
            if symbol:
                try:
                    from src.doc_claims import _ast_has_symbol
                    if full.endswith(".py") and not _ast_has_symbol(full, symbol):
                        issues.append({"ref": ref, "why": "symbol_not_found"})
                except Exception:
                    logger.debug("project_concepts.stale_check: symbol grounding failed", exc_info=True)
        return {"stale": bool(issues), "checked": True, "issues": issues}


# ---------------------------------------------------------------------------
# Module-level convenience (project_id/workspace resolved once, mirrors
# `requirements`'s package-level functions over its own `Store`)
# ---------------------------------------------------------------------------
def _store(*, project_id: Optional[str] = None, workspace: Optional[str] = None,
           project_key: Optional[str] = None) -> Store:
    key = project_key or resolve_project_key(project_id=project_id, workspace=workspace)
    return Store(key)

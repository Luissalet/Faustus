"""Excursos (side threads) — pure logic + DB access, no FastAPI.

CONTRATO_EXCURSOS.md, Lote A. The idea (from ThoughtDAG): a side thread is a
normal Faustus session wired to its "parent" by a `branch` cable
(`core.database.SessionWire`); what the model sees is exactly what is
cabled to the node, nothing more. A side thread never copies the parent's
messages the way `/fork` does — `inherited_context` (the hook this module
exists for) reads the wire back at prompt-build time instead, so the parent
transcript is the single source of truth for everything downstream of it.

Every public function here takes `owner: Optional[str]`. When it is not
None, any session the function reads must belong to that owner — a session
belonging to someone else is treated exactly like a session that does not
exist (never leaked, never distinguished in an error). `owner=None` means
single-user / unscoped mode, matching how `routes.session_routes.
_verify_session_owner` and the rest of the codebase treat the nullable
`sessions.owner` column.

No function in this module is ever called by agent tooling — every wire is
created by a human through the HTTP routes in `routes/side_thread_routes.py`
(CONTRATO_EXCURSOS: "ningún cable se crea sin que lo pida un humano por la
API; ninguna tool de agente").
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from core.database import (
    ChatMessage as DbChatMessage,
    Session as DbSession,
    SessionLocal,
    SessionWire as DbSessionWire,
)
from src.model_context import estimate_tokens

logger = logging.getLogger(__name__)

# Recursion / traversal ceilings (CONTRATO_EXCURSOS: "Profundidad máx 8" for
# inherited context, "Profundidad máxima 32" for the thought map). Bounding
# these is not an optimization — a corrupted or (despite the invariant)
# cyclic wire graph must degrade to a truncated result, never an infinite
# loop inside a chat request.
MAX_INHERITED_DEPTH = 8
MAX_THOUGHT_MAP_DEPTH = 32

_MISSING_ANCHOR_NOTE = (
    "[Note: the turn this side thread branched from no longer exists in the "
    "parent conversation; the whole remaining parent transcript is included "
    "instead]"
)


class SideThreadError(Exception):
    """A request the caller must fix — the routes hand this back verbatim as
    the flat ``{"error", "error_class"}`` body CONTRATO_EXCURSOS requires,
    so this module never has to know about HTTP and the routes never have
    to re-derive a status code or message from a generic exception.
    """

    def __init__(self, message: str, error_class: str, status: int = 400):
        super().__init__(message)
        self.error_class = error_class
        self.status = status


# ---------------------------------------------------------------------------
# Small DB-only helpers shared by every function below
# ---------------------------------------------------------------------------

def _owned_row(db, session_id: Optional[str], owner: Optional[str]) -> Optional[DbSession]:
    """The `sessions` row for `session_id`, or None if absent or another
    owner's — the "treated as inexistent" rule applied at the DB layer."""
    if not session_id:
        return None
    row = db.query(DbSession).filter(DbSession.id == session_id).first()
    if row is None:
        return None
    if owner is not None and row.owner != owner:
        return None
    return row


def _owned_session_via_manager(session_manager, owner: Optional[str], session_id: Optional[str]):
    """`session_manager.get_session(session_id)`, owner-checked, tolerant of
    a missing/foreign session (returns None rather than raising) — every
    caller here treats "not mine" and "does not exist" identically."""
    if session_manager is None or not session_id:
        return None
    try:
        sess = session_manager.get_session(session_id)
    except KeyError:
        return None
    except Exception:
        logger.debug("side_threads: get_session(%s) failed", session_id, exc_info=True)
        return None
    if sess is None:
        return None
    if owner is not None and getattr(sess, "owner", None) != owner:
        return None
    return sess


def _wire_to_dict(wire: DbSessionWire) -> Dict[str, Any]:
    return {
        "id": wire.id,
        "kind": wire.kind,
        "owner": wire.owner,
        "source_session_id": wire.source_session_id,
        "target_session_id": wire.target_session_id,
        "anchor_index": wire.anchor_index,
        "anchor_passage": wire.anchor_passage,
        "depth": wire.depth,
        "context_order": wire.context_order,
        "archived": bool(wire.archived),
        "source_fingerprint": wire.source_fingerprint,
        "created_at": wire.created_at.isoformat() if wire.created_at else None,
    }


def _is_slash(row_or_msg) -> bool:
    """True for a persisted slash-command reply (`metadata.source == 'slash'`
    on a `core.models.ChatMessage`, or the equivalent parsed
    `chat_messages.metadata` JSON on a raw DB row) — UI chatter that never
    reaches the model, mirroring `core.models.Session.get_context_messages`.
    """
    meta = getattr(row_or_msg, "metadata", None)
    if meta is None:
        raw = getattr(row_or_msg, "meta_data", None)
        if raw:
            try:
                meta = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                meta = None
    return isinstance(meta, dict) and meta.get("source") == "slash"


def _history_rows(session_id: str) -> List[DbChatMessage]:
    """Every `chat_messages` row for a session, oldest first, straight from
    the DB — used by the DB-only functions (`add_reference`, `wires_for`,
    chain titles) that have no `SessionManager` to hydrate through. Side
    threads are expected to stay small, so reading the whole transcript here
    is simplicity over a micro-optimization, not a scalability bet.
    """
    db = SessionLocal()
    try:
        return (
            db.query(DbChatMessage)
            .filter(DbChatMessage.session_id == session_id)
            .order_by(DbChatMessage.timestamp)
            .all()
        )
    finally:
        db.close()


def _first_user_title(rows: List[DbChatMessage]) -> Optional[str]:
    for row in rows:
        if row.role != "user" or _is_slash(row):
            continue
        content = row.content if isinstance(row.content, str) else str(row.content or "")
        return content[:60]
    return None


def _chain_titles(session_id: str, max_depth: int = MAX_INHERITED_DEPTH) -> List[str]:
    """Root-to-leaf opening questions for a side thread's ancestor chain —
    the `Trail (upstream questions): q1 → q2 → q3` line in `reference_block`.

    Walks `branch` wires upward from `session_id` itself (so the chain's own
    opening question is the LAST entry, closest to the quoted exchange),
    through each ancestor excurso, collecting the first non-slash user
    message of each hop. Self-contained (opens its own DB session) since it
    is reused from both DB-only and SessionManager-backed callers, and it is
    pure text — nothing here needs SessionManager's caching semantics.
    """
    chain: List[str] = []
    seen = set()
    current = session_id
    depth = 0
    db = SessionLocal()
    try:
        while current and depth < max_depth and current not in seen:
            seen.add(current)
            rows = (
                db.query(DbChatMessage)
                .filter(DbChatMessage.session_id == current)
                .order_by(DbChatMessage.timestamp)
                .all()
            )
            title = _first_user_title(rows)
            if title:
                chain.append(title)
            wire = (
                db.query(DbSessionWire)
                .filter(DbSessionWire.kind == "branch", DbSessionWire.target_session_id == current)
                .first()
            )
            if wire is None:
                break
            current = wire.source_session_id
            depth += 1
    finally:
        db.close()
    chain.reverse()
    return chain


def _reference_block_for_db(session_id: str, name: str, depth: str) -> str:
    """DB-only convenience: load a session's (slash-filtered) transcript and
    render its `[Reference: ...]` block, for callers (`add_reference`,
    `wires_for`) that have no `SessionManager` handy."""
    rows = [r for r in _history_rows(session_id) if not _is_slash(r)]
    chain_titles = _chain_titles(session_id) if depth == "quote" else []
    return reference_block(SimpleNamespace(name=name, history=rows), depth, chain_titles)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def fingerprint(session) -> str:
    """Short content identity of a session's transcript, for staleness
    detection on a `reference` wire.

    Hashes the total message count plus the last two messages' role+content
    (12 hex chars of sha256) — cheap enough to run on every `wires_for` call.
    Growing the side thread, or editing its tail, changes the hash; renaming
    the session or editing an earlier message does not. `session` needs only
    a `.history` of role/content-bearing entries (a `core.models.ChatMessage`
    list, a list of dicts, or raw `core.database.ChatMessage` rows all work).
    """
    history = list(getattr(session, "history", None) or [])
    parts = [str(len(history))]
    for msg in history[-2:]:
        role = getattr(msg, "role", None)
        if role is None and isinstance(msg, dict):
            role = msg.get("role")
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        parts.append(f"{role}:{content}")
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8", "replace")).hexdigest()
    return digest[:12]


def reference_block(source_session, depth: str, chain_titles: List[str]) -> str:
    """Render one `[Reference: ...]` block — the sole channel a side
    thread's content re-enters another conversation through (CONTRATO_
    EXCURSOS: "un bloque de referencia explícito", never an automatic
    replay or a raw history splice).

    Pure: reads only `source_session.name` and `.history` (role/content
    entries, already slash-filtered by the caller), so it is testable
    without touching the database. `depth='full'` renders every Q/A pair
    with no trail; the default `'quote'` renders the trail (when
    `chain_titles` is non-empty) plus only the latest exchange.
    """
    name = getattr(source_session, "name", "") or ""
    history = list(getattr(source_session, "history", None) or [])
    lines = [f"[Reference: {name}]"]

    def _role(msg):
        role = getattr(msg, "role", None)
        if role is None and isinstance(msg, dict):
            role = msg.get("role")
        return role

    def _text(msg):
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        return content if isinstance(content, str) else str(content if content is not None else "")

    if depth == "full":
        for msg in history:
            role = _role(msg)
            if role == "user":
                lines.append(f"Q: {_text(msg)}")
            elif role == "assistant":
                lines.append(f"A: {_text(msg)}")
        return "\n".join(lines)

    # 'quote' (default): trail of upstream questions, then only the latest exchange.
    if chain_titles:
        lines.append("Trail (upstream questions): " + " → ".join(chain_titles))
    last_user = next((m for m in reversed(history) if _role(m) == "user"), None)
    last_assistant = next((m for m in reversed(history) if _role(m) == "assistant"), None)
    if last_user is not None:
        lines.append(f"Q: {_text(last_user)}")
    if last_assistant is not None:
        lines.append(f"A: {_text(last_assistant)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Creating a side thread
# ---------------------------------------------------------------------------

def create_side_thread(
    session_manager,
    owner: Optional[str],
    parent_id: str,
    anchor_index: int,
    passage: Optional[str] = None,
    question: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a brand-new session wired to `parent_id` by a `branch` cable.

    Unlike `/api/session/{id}/fork` (`routes/history/history_routes.py`),
    NOTHING is copied into the new session's history — inheritance is
    entirely through the wire, read back by `inherited_context` at
    prompt-build time. The new session starts empty and the parent's own
    history is never touched (byte-for-byte, message-for-message).

    `question`, when given, is returned as-is (never sent to any model
    here) — the caller (the route) hands it back for the Studio to seed the
    new session's composer with.
    """
    parent = _owned_session_via_manager(session_manager, owner, parent_id)
    if parent is None:
        raise SideThreadError(f"Session {parent_id} not found", "excursos.not_found", 404)

    history_len = len(parent.history or [])
    if (
        not isinstance(anchor_index, int)
        or isinstance(anchor_index, bool)
        or anchor_index < 0
        or anchor_index >= history_len
    ):
        raise SideThreadError(
            f"anchor_index {anchor_index!r} is out of range for a "
            f"{history_len}-message parent history",
            "excursos.anchor_out_of_range",
            400,
        )

    passage_clean = str(passage)[:2000] if passage else None
    base = passage_clean or (str(question) if question else None) or (parent.name or "")
    new_name = "↳ " + (base[:48] if base else (parent.name or ""))
    new_id = str(uuid.uuid4())
    parent_owner = getattr(parent, "owner", None)

    session_manager.create_session(
        session_id=new_id,
        name=new_name,
        endpoint_url=parent.endpoint_url,
        model=parent.model,
        rag=False,
        owner=parent_owner,
        folder=getattr(parent, "folder", None),
        mode=getattr(parent, "mode", None),
        project_id=getattr(parent, "project_id", None),
    )

    db = SessionLocal()
    try:
        wire = DbSessionWire(
            id=str(uuid.uuid4()),
            owner=parent_owner,
            kind="branch",
            source_session_id=parent_id,
            target_session_id=new_id,
            anchor_index=anchor_index,
            anchor_passage=passage_clean,
            depth="quote",
            context_order=0,
            archived=False,
            source_fingerprint=None,
            created_at=datetime.now(timezone.utc),
        )
        db.add(wire)
        db.commit()
        db.refresh(wire)
        wire_dict = _wire_to_dict(wire)
    finally:
        db.close()

    try:
        from src.event_bus import fire_event
        fire_event("session_created", parent_owner)
    except Exception:
        logger.debug("session_created event dispatch failed for side thread", exc_info=True)

    return {"session_id": new_id, "wire": wire_dict}


# ---------------------------------------------------------------------------
# References ("traer de vuelta")
# ---------------------------------------------------------------------------

def add_reference(owner: Optional[str], source_id: str, target_id: str, depth: str = "quote") -> Dict[str, Any]:
    """Wire `source_id` (a side thread) back into `target_id` as one
    `[Reference: ...]` block. Idempotent: an existing non-archived
    `reference` wire between the same pair is returned unchanged rather than
    duplicated — clicking "Traer de vuelta" twice is a no-op, not two blocks.
    """
    if source_id == target_id:
        raise SideThreadError("A session cannot reference itself", "excursos.self_reference", 400)
    if depth not in ("quote", "full"):
        raise SideThreadError(f"Unknown reference depth: {depth!r}", "excursos.bad_depth", 400)

    db = SessionLocal()
    try:
        source_row = _owned_row(db, source_id, owner)
        target_row = _owned_row(db, target_id, owner)
        if source_row is None or target_row is None:
            raise SideThreadError("Session not found", "excursos.not_found", 404)

        existing = (
            db.query(DbSessionWire)
            .filter(
                DbSessionWire.kind == "reference",
                DbSessionWire.source_session_id == source_id,
                DbSessionWire.target_session_id == target_id,
                DbSessionWire.archived == False,  # noqa: E712
            )
            .first()
        )
        if existing is not None:
            wire = existing
        else:
            max_order = (
                db.query(func.max(DbSessionWire.context_order))
                .filter(DbSessionWire.kind == "reference", DbSessionWire.target_session_id == target_id)
                .scalar()
            )
            next_order = (max_order + 1) if max_order is not None else 0
            wire = DbSessionWire(
                id=str(uuid.uuid4()),
                owner=source_row.owner if source_row.owner is not None else owner,
                kind="reference",
                source_session_id=source_id,
                target_session_id=target_id,
                depth=depth,
                context_order=next_order,
                archived=False,
                source_fingerprint=fingerprint(SimpleNamespace(history=_history_rows(source_id))),
                created_at=datetime.now(timezone.utc),
            )
            db.add(wire)
            db.commit()
            db.refresh(wire)
        wire_dict = _wire_to_dict(wire)
        source_name = source_row.name
    finally:
        db.close()

    # `tokens` (route response shape: {"wire","tokens"}) needs the rendered
    # block, which needs the source's own transcript — computed after the
    # write commits so a slow render never holds the DB session open.
    block = _reference_block_for_db(source_id, source_name, wire_dict["depth"])
    tokens = estimate_tokens([{"role": "user", "content": block}])
    return {"wire": wire_dict, "tokens": tokens}


def update_reference(
    owner: Optional[str],
    wire_id: str,
    *,
    depth: Optional[str] = None,
    archived: Optional[bool] = None,
    context_order: Optional[int] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Patch a `reference` wire. `refresh=True` recomputes
    `source_fingerprint` from the side thread's CURRENT tail — the user
    "accepting" a side thread that grew since it was last wired in, clearing
    `stale`."""
    if depth is not None and depth not in ("quote", "full"):
        raise SideThreadError(f"Unknown reference depth: {depth!r}", "excursos.bad_depth", 400)

    db = SessionLocal()
    try:
        wire = (
            db.query(DbSessionWire)
            .filter(DbSessionWire.id == wire_id, DbSessionWire.kind == "reference")
            .first()
        )
        if wire is None:
            raise SideThreadError(f"Reference {wire_id} not found", "excursos.not_found", 404)
        if owner is not None:
            if _owned_row(db, wire.source_session_id, owner) is None or _owned_row(db, wire.target_session_id, owner) is None:
                raise SideThreadError(f"Reference {wire_id} not found", "excursos.not_found", 404)

        if depth is not None:
            wire.depth = depth
        if archived is not None:
            wire.archived = bool(archived)
        if context_order is not None:
            wire.context_order = int(context_order)
        if refresh:
            wire.source_fingerprint = fingerprint(SimpleNamespace(history=_history_rows(wire.source_session_id)))

        db.commit()
        db.refresh(wire)
        return {"wire": _wire_to_dict(wire)}
    finally:
        db.close()


def remove_reference(owner: Optional[str], wire_id: str) -> bool:
    """DELETE a `reference` wire physically. The side thread it pointed at
    is untouched and keeps existing — only the cable is gone."""
    db = SessionLocal()
    try:
        wire = (
            db.query(DbSessionWire)
            .filter(DbSessionWire.id == wire_id, DbSessionWire.kind == "reference")
            .first()
        )
        if wire is None:
            return False
        if owner is not None:
            if _owned_row(db, wire.source_session_id, owner) is None or _owned_row(db, wire.target_session_id, owner) is None:
                return False
        db.delete(wire)
        db.commit()
        return True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reading the graph
# ---------------------------------------------------------------------------

def wires_for(owner: Optional[str], session_id: str) -> Dict[str, Any]:
    """Everything the Studio's wiring panel needs for one session: its
    parent (if it is a side thread), its own children, and the references
    flowing in and out. DB-only (no `SessionManager`) — every field here is
    derivable straight from `sessions` / `chat_messages`, and a read-only
    panel should reflect the database, not whatever happens to be cached.
    """
    db = SessionLocal()
    try:
        self_row = _owned_row(db, session_id, owner)
        if self_row is None:
            raise SideThreadError(f"Session {session_id} not found", "excursos.not_found", 404)

        parent = None
        parent_wire = (
            db.query(DbSessionWire)
            .filter(DbSessionWire.kind == "branch", DbSessionWire.target_session_id == session_id)
            .first()
        )
        if parent_wire is not None:
            parent_row = _owned_row(db, parent_wire.source_session_id, owner)
            if parent_row is not None:
                parent_len = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == parent_row.id)
                    .count()
                )
                anchor_index = parent_wire.anchor_index
                anchor_state = "ok" if isinstance(anchor_index, int) and anchor_index < parent_len else "missing"
                parent = {
                    "wire": _wire_to_dict(parent_wire),
                    "session": {"id": parent_row.id, "name": parent_row.name},
                    "anchor_state": anchor_state,
                }

        children = []
        child_wires = (
            db.query(DbSessionWire)
            .filter(DbSessionWire.kind == "branch", DbSessionWire.source_session_id == session_id)
            .order_by(DbSessionWire.created_at)
            .all()
        )
        for w in child_wires:
            child_row = _owned_row(db, w.target_session_id, owner)
            if child_row is None:
                continue
            ref_wire = (
                db.query(DbSessionWire)
                .filter(
                    DbSessionWire.kind == "reference",
                    DbSessionWire.source_session_id == w.target_session_id,
                    DbSessionWire.target_session_id == session_id,
                    DbSessionWire.archived == False,  # noqa: E712
                )
                .first()
            )
            stale = False
            if ref_wire is not None:
                child_fp = fingerprint(SimpleNamespace(history=_history_rows(child_row.id)))
                stale = child_fp != ref_wire.source_fingerprint
            children.append({
                "wire": _wire_to_dict(w),
                "session": {"id": child_row.id, "name": child_row.name, "message_count": child_row.message_count or 0},
                "reference": _wire_to_dict(ref_wire) if ref_wire is not None else None,
                "stale": stale,
            })

        references_in = []
        ref_in_wires = (
            db.query(DbSessionWire)
            .filter(
                DbSessionWire.kind == "reference",
                DbSessionWire.target_session_id == session_id,
                DbSessionWire.archived == False,  # noqa: E712
            )
            .order_by(DbSessionWire.context_order)
            .all()
        )
        for w in ref_in_wires:
            src_row = _owned_row(db, w.source_session_id, owner)
            if src_row is None:
                continue
            src_fp = fingerprint(SimpleNamespace(history=_history_rows(src_row.id)))
            block = _reference_block_for_db(src_row.id, src_row.name, w.depth)
            references_in.append({
                "wire": _wire_to_dict(w),
                "session": {"id": src_row.id, "name": src_row.name},
                "stale": src_fp != w.source_fingerprint,
                "tokens": estimate_tokens([{"role": "user", "content": block}]),
            })

        references_out = []
        ref_out_wires = (
            db.query(DbSessionWire)
            .filter(
                DbSessionWire.kind == "reference",
                DbSessionWire.source_session_id == session_id,
                DbSessionWire.archived == False,  # noqa: E712
            )
            .order_by(DbSessionWire.context_order)
            .all()
        )
        for w in ref_out_wires:
            tgt_row = _owned_row(db, w.target_session_id, owner)
            if tgt_row is None:
                continue
            references_out.append({
                "wire": _wire_to_dict(w),
                "session": {"id": tgt_row.id, "name": tgt_row.name},
            })

        return {
            "parent": parent,
            "children": children,
            "references_in": references_in,
            "references_out": references_out,
        }
    finally:
        db.close()


def thought_map(owner: Optional[str], session_id: str) -> Dict[str, Any]:
    """The `branch` tree from the root excurso down, with `session_id`
    marked `"current": true`. Depth-capped at `MAX_THOUGHT_MAP_DEPTH` in
    both directions (walking up to the root, and back down) — beyond that
    the result carries `"truncated": true` rather than growing unbounded.
    """
    db = SessionLocal()
    try:
        self_row = _owned_row(db, session_id, owner)
        if self_row is None:
            raise SideThreadError(f"Session {session_id} not found", "excursos.not_found", 404)

        # Walk up to the root.
        root_id = session_id
        seen = {session_id}
        hops = 0
        truncated = False
        current = session_id
        while hops < MAX_THOUGHT_MAP_DEPTH:
            wire = (
                db.query(DbSessionWire)
                .filter(DbSessionWire.kind == "branch", DbSessionWire.target_session_id == current)
                .first()
            )
            if wire is None:
                root_id = current
                break
            if wire.source_session_id in seen:
                # A cycle should be structurally impossible (see SessionWire's
                # docstring); if the data is ever corrupted, stop rather than
                # loop forever.
                root_id = current
                truncated = True
                break
            seen.add(wire.source_session_id)
            current = wire.source_session_id
            hops += 1
        else:
            root_id = current
            truncated = True

        truncated_flag = {"value": truncated}
        visited_down: set = set()

        def _build(node_id: str, depth: int, anchor_index: Optional[int]):
            row = _owned_row(db, node_id, owner)
            if row is None or node_id in visited_down:
                return None
            visited_down.add(node_id)
            node: Dict[str, Any] = {
                "id": row.id,
                "name": row.name,
                "message_count": row.message_count or 0,
                "anchor_index": anchor_index,
                "children": [],
            }
            if node_id == session_id:
                node["current"] = True
            if depth >= MAX_THOUGHT_MAP_DEPTH:
                truncated_flag["value"] = True
                return node
            child_wires = (
                db.query(DbSessionWire)
                .filter(DbSessionWire.kind == "branch", DbSessionWire.source_session_id == node_id)
                .order_by(DbSessionWire.created_at)
                .all()
            )
            for w in child_wires:
                child_node = _build(w.target_session_id, depth + 1, w.anchor_index)
                if child_node is not None:
                    node["children"].append(child_node)
            return node

        tree = _build(root_id, 0, None)
        result: Dict[str, Any] = tree if tree is not None else {}
        if truncated_flag["value"]:
            result["truncated"] = True
        return result
    finally:
        db.close()


def parents_map(owner: Optional[str]) -> Dict[str, str]:
    """`{child_session_id: parent_session_id}` for every `branch` wire this
    owner can see — lets the Studio's session list indent side threads under
    their parent without an N+1 lookup per row."""
    db = SessionLocal()
    try:
        q = db.query(DbSessionWire.target_session_id, DbSessionWire.source_session_id).filter(
            DbSessionWire.kind == "branch"
        )
        if owner is not None:
            q = q.filter(DbSessionWire.owner == owner)
        return {child: parent for child, parent in q.all()}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The hook: inherited_context + context_preview
# ---------------------------------------------------------------------------

def _reference_blocks(session_manager, owner: Optional[str], session_id: str, *, rows=None) -> List[Dict[str, Any]]:
    """Step 1 of `inherited_context`: one `[Reference]` block per non-archived
    `reference` wire pointed AT `session_id`, in `context_order`."""
    if rows is None:
        db = SessionLocal()
        try:
            rows = (
                db.query(DbSessionWire)
                .filter(
                    DbSessionWire.kind == "reference",
                    DbSessionWire.target_session_id == session_id,
                    DbSessionWire.archived == False,  # noqa: E712
                )
                .order_by(DbSessionWire.context_order)
                .all()
            )
        finally:
            db.close()
    else:
        rows = sorted(
            (w for w in rows if w.kind == "reference" and not w.archived),
            key=lambda w: w.context_order,
        )

    blocks: List[Dict[str, Any]] = []
    for w in rows:
        source = _owned_session_via_manager(session_manager, owner, w.source_session_id)
        if source is None:
            continue
        chain_titles = _chain_titles(source.id) if w.depth == "quote" else []
        text = reference_block(
            SimpleNamespace(name=source.name, history=source.get_context_messages()),
            w.depth,
            chain_titles,
        )
        blocks.append({"role": "user", "content": text})
    return blocks


def _branch_chain(
    session_manager,
    owner: Optional[str],
    session_id: str,
    *,
    _depth: int = 0,
    _seen: Optional[set] = None,
    wire: Optional[DbSessionWire] = None,
) -> List[Dict[str, Any]]:
    """Steps 2+3 of `inherited_context`: the `branch` parent's own history up
    to (and including) the anchor, recursively prefixed by the parent's OWN
    inherited context when the parent is itself a side thread, then the
    `anchor_passage` note if the wire carries one.
    """
    if _depth >= MAX_INHERITED_DEPTH:
        return []
    seen = set(_seen) if _seen else set()
    if session_id in seen:
        return []
    seen.add(session_id)

    if wire is None:
        db = SessionLocal()
        try:
            wire = (
                db.query(DbSessionWire)
                .filter(DbSessionWire.kind == "branch", DbSessionWire.target_session_id == session_id)
                .first()
            )
        finally:
            db.close()
    if wire is None:
        return []

    parent = _owned_session_via_manager(session_manager, owner, wire.source_session_id)
    if parent is None:
        return []

    parent_history = parent.history or []
    anchor_index = wire.anchor_index
    anchor_state = "ok" if isinstance(anchor_index, int) and anchor_index < len(parent_history) else "missing"

    upstream = _branch_chain(session_manager, owner, parent.id, _depth=_depth + 1, _seen=seen)

    def _fresh(msg) -> Dict[str, Any]:
        # A NEW dict every time — never a `ChatMessage.to_dict()` the parent
        # stamped `_history_index` onto. See `inherited_context`'s docstring:
        # this is what lets compaction summarize inherited messages in the
        # prompt without ever being able to delete a row because of them.
        return {"role": getattr(msg, "role", None), "content": getattr(msg, "content", None)}

    if anchor_state == "ok":
        own = [_fresh(m) for m in parent_history[: anchor_index + 1] if not _is_slash(m)]
    else:
        own = [_fresh(m) for m in parent_history if not _is_slash(m)]
        own.append({"role": "user", "content": _MISSING_ANCHOR_NOTE})

    result = upstream + own
    if wire.anchor_passage:
        result.append({"role": "user", "content": f'[Regarding this passage: "{wire.anchor_passage}"]'})
    return result


def inherited_context(session_manager, owner: Optional[str], session_id: str, *, _depth: int = 0) -> List[Dict[str, Any]]:
    """THE hook. Messages that go BEFORE a session's own history in the
    prompt (`routes/chat_helpers.py::build_chat_context`:
    ``messages = preface + inherited_context(...) + _history_messages``).

    Two independent layers, concatenated:
    1. Explicit `reference` wires pointed AT this session — one
       `[Reference: ...]` block per source, in `context_order`.
    2. The `branch` cable this session was created from, if any — the
       parent's own transcript up to the anchor, recursively through any
       grandparent excursos (a side thread whose parent is itself a side
       thread), plus the `[Regarding this passage: ...]` note when the wire
       carries a quoted passage.

    Every dict returned here is freshly built (see `_branch_chain`'s
    `_fresh`), never a `ChatMessage.to_dict()` already stamped with
    `src.context_compactor.HISTORY_INDEX_KEY` — so compaction may summarize
    these messages in the prompt, but can never delete a row from ANY
    session because of them.

    No wires at all → `[]`, touching the database at most once (a single
    query for every wire targeting `session_id`, partitioned in Python).
    Never raises: any failure here must not break the chat turn it was
    invoked from — the caller (`build_chat_context`) already wraps the call
    in a broad `except Exception`, but this degrades to `[]` itself too, so
    every OTHER caller (`context_preview`, tests) gets the same guarantee.
    """
    if session_manager is None or not session_id or _depth >= MAX_INHERITED_DEPTH:
        return []
    try:
        db = SessionLocal()
        try:
            rows = db.query(DbSessionWire).filter(DbSessionWire.target_session_id == session_id).all()
        finally:
            db.close()

        branch_wire = next((w for w in rows if w.kind == "branch"), None)
        has_ref = any(w.kind == "reference" and not w.archived for w in rows)
        if branch_wire is None and not has_ref:
            return []

        refs = _reference_blocks(session_manager, owner, session_id, rows=rows)
        branch = (
            _branch_chain(session_manager, owner, session_id, _depth=_depth, wire=branch_wire)
            if branch_wire is not None
            else []
        )
        return refs + branch
    except Exception:
        logger.debug("side_threads.inherited_context failed for session %s", session_id, exc_info=True)
        return []


def context_preview(session_manager, owner: Optional[str], session_id: str) -> Dict[str, Any]:
    """What the model will see on the next turn, broken into the three
    layers the Studio's "Qué verá el modelo" panel renders."""
    self_sess = _owned_session_via_manager(session_manager, owner, session_id)
    if self_sess is None:
        raise SideThreadError(f"Session {session_id} not found", "excursos.not_found", 404)

    db = SessionLocal()
    try:
        parent_wire = (
            db.query(DbSessionWire)
            .filter(DbSessionWire.kind == "branch", DbSessionWire.target_session_id == session_id)
            .first()
        )
        ref_wires = (
            db.query(DbSessionWire)
            .filter(
                DbSessionWire.kind == "reference",
                DbSessionWire.target_session_id == session_id,
                DbSessionWire.archived == False,  # noqa: E712
            )
            .order_by(DbSessionWire.context_order)
            .all()
        )
    finally:
        db.close()

    ref_blocks = _reference_blocks(session_manager, owner, session_id, rows=ref_wires)
    ref_items = []
    for w in ref_wires:
        src = _owned_session_via_manager(session_manager, owner, w.source_session_id)
        if src is None:
            continue
        stale = fingerprint(SimpleNamespace(history=src.history)) != w.source_fingerprint
        ref_items.append({"session_id": src.id, "name": src.name, "depth": w.depth, "stale": stale})

    branch_msgs = (
        _branch_chain(session_manager, owner, session_id, wire=parent_wire)
        if parent_wire is not None
        else []
    )
    inherited_from = None
    if parent_wire is not None:
        parent = _owned_session_via_manager(session_manager, owner, parent_wire.source_session_id)
        if parent is not None:
            anchor_index = parent_wire.anchor_index
            anchor_state = "ok" if isinstance(anchor_index, int) and anchor_index < len(parent.history or []) else "missing"
            inherited_from = {
                "session_id": parent.id,
                "name": parent.name,
                "anchor_index": anchor_index,
                "anchor_state": anchor_state,
            }

    own_msgs = self_sess.get_context_messages()

    references_layer = {
        "layer": "references",
        "messages": len(ref_blocks),
        "tokens": estimate_tokens(ref_blocks),
        "items": ref_items,
    }
    inherited_layer = {
        "layer": "inherited",
        "messages": len(branch_msgs),
        "tokens": estimate_tokens(branch_msgs),
        "from": inherited_from,
    }
    own_layer = {
        "layer": "own",
        "messages": len(own_msgs),
        "tokens": estimate_tokens(own_msgs),
    }
    total_tokens = references_layer["tokens"] + inherited_layer["tokens"] + own_layer["tokens"]
    return {"layers": [references_layer, inherited_layer, own_layer], "total_tokens": total_tokens}

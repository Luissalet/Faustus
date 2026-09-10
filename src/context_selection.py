"""src/context_selection.py — CTX-05: recuperacion selectiva con control del usuario.

Three scopes, kept separate the way the backlog asks ("Ambitos de sesion,
proyecto y memoria global separados"): a control saved with a `session_id`
applies only inside that one conversation, one saved with a `project_id` and
no session applies to every session of that project, and one saved with
neither is the owner's global default. `list_controls()` merges broad-to-
narrow (global, then project, then session) rather than picking one — a
session exclusion adds to the project's, it never silently replaces it, and
nothing here ever reaches into a session that did not ask for it: the
acceptance criterion this exists to satisfy is exactly "excluir una memoria
no [...] se aplica por error al historial completo".

Two kinds of control: `exclude`/`exclude_prefix` ("no usar esta fuente", a
whole file or a folder) and `pin` ("usar este fragmento"). Neither is a
delete — `unset_control()` is the only way rows here disappear, and nothing
in this module ever opens `src.upload_handler`, `src.database` or any other
authority that could touch the source file itself. What consumes these rows
is `src.context_engine.ranking.validate()` (`policy.excluded_refs`/
`excluded_prefixes`, CTX-05's actual enforcement point) and
`src.context_engine.wiring.build_request()`, which now reads this module so a
turn built through the normal path picks the controls up without every
caller having to remember to.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from src.context_engine import store

logger = logging.getLogger(__name__)

KIND_EXCLUDE = "exclude"
KIND_EXCLUDE_PREFIX = "exclude_prefix"
KIND_PIN = "pin"
KINDS: Tuple[str, ...] = (KIND_EXCLUDE, KIND_EXCLUDE_PREFIX, KIND_PIN)

MAX_REF_CHARS = 2048

store.register_schema("context_selection_controls", (
    """
    CREATE TABLE IF NOT EXISTS selection_controls (
        owner       TEXT NOT NULL DEFAULT '',
        project_id  TEXT NOT NULL DEFAULT '',
        session_id  TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL,
        ref         TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, project_id, session_id, kind, ref)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_selection_controls_owner "
    "ON selection_controls(owner, project_id, session_id)",
))


def _norm(value: Any, limit: int = MAX_REF_CHARS) -> str:
    return str(value or "").strip()[:limit]


def set_control(owner: str, kind: str, ref: str, *, project_id: str = "",
                session_id: str = "") -> Dict[str, Any]:
    """Add one control. Idempotent — setting the same one twice is a no-op,
    not a duplicate row (the primary key already guarantees that)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    owner_n, project_n, session_n, ref_n = (
        _norm(owner, 256), _norm(project_id, 200), _norm(session_id, 200), _norm(ref))
    if not owner_n or not ref_n:
        raise ValueError("owner and ref are required")
    with store.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO selection_controls "
            "(owner, project_id, session_id, kind, ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (owner_n, project_n, session_n, kind, ref_n, store.now_iso()))
    return {"owner": owner_n, "project_id": project_n, "session_id": session_n,
            "kind": kind, "ref": ref_n}


def unset_control(owner: str, kind: str, ref: str, *, project_id: str = "",
                  session_id: str = "") -> bool:
    """Remove one control. Never touches the source it named — this table
    only ever says whether a ref is offered to retrieval, nothing about
    whether the file/memory itself exists."""
    owner_n, project_n, session_n, ref_n = (
        _norm(owner, 256), _norm(project_id, 200), _norm(session_id, 200), _norm(ref))
    with store.db() as conn:
        cur = conn.execute(
            "DELETE FROM selection_controls WHERE owner = ? AND project_id = ? "
            "AND session_id = ? AND kind = ? AND ref = ?",
            (owner_n, project_n, session_n, kind, ref_n))
        return cur.rowcount > 0


def list_controls(owner: str, *, project_id: str = "",
                  session_id: str = "") -> Dict[str, List[Dict[str, str]]]:
    """Every control in play for this exact scope triple, broad to narrow:
    global (no project, no session) + this project (no session) + this
    session. A DIFFERENT session's rows, or a different project's, never
    appear — that is the isolation the acceptance criterion asks for."""
    owner_n, project_n, session_n = _norm(owner, 256), _norm(project_id, 200), _norm(session_id, 200)
    out: Dict[str, List[Dict[str, str]]] = {kind: [] for kind in KINDS}
    scopes = [("", "")]
    if project_n:
        scopes.append((project_n, ""))
    if session_n:
        scopes.append((project_n, session_n))
    try:
        with store.db() as conn:
            for proj, sess in scopes:
                rows = conn.execute(
                    "SELECT kind, ref, project_id, session_id FROM selection_controls "
                    "WHERE owner = ? AND project_id = ? AND session_id = ?",
                    (owner_n, proj, sess)).fetchall()
                for row in rows:
                    out.setdefault(row["kind"], []).append({
                        "ref": row["ref"], "project_id": row["project_id"],
                        "session_id": row["session_id"]})
    except store.ContextStoreError as exc:
        logger.warning("context_selection: store unavailable for %s: %s", owner_n, exc)
    return out


def policy_overrides(owner: str, *, project_id: str = "",
                     session_id: str = "") -> Dict[str, Tuple[str, ...]]:
    """`{"excluded_refs", "excluded_prefixes", "pinned_refs"}` ready to merge
    into a `ContextPolicy`/`explicit_refs` — deduplicated, order preserved."""
    controls = list_controls(owner, project_id=project_id, session_id=session_id)

    def refs(kind: str) -> Tuple[str, ...]:
        seen: List[str] = []
        for row in controls.get(kind) or ():
            ref = row.get("ref") or ""
            if ref and ref not in seen:
                seen.append(ref)
        return tuple(seen)

    return {
        "excluded_refs": refs(KIND_EXCLUDE),
        "excluded_prefixes": refs(KIND_EXCLUDE_PREFIX),
        "pinned_refs": refs(KIND_PIN),
    }


__all__ = [
    "KIND_EXCLUDE", "KIND_EXCLUDE_PREFIX", "KIND_PIN", "KINDS",
    "set_control", "unset_control", "list_controls", "policy_overrides",
]

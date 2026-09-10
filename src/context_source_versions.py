"""src/context_source_versions.py — CTX-04: cache correctness across a moving source.

The failure this closes: a file gets edited (or a project stops sharing it —
"revocar su acceso" in the backlog's own words) and something built from its
old bytes keeps being served because nothing told the cache the ground moved.
`src/context_engine/cache.py` already has the *mechanism* for this — §1.7's
event table and `on_event()` — but nothing in the repository called it for a
plain source edit; `note_event()`/`on_event()` had exactly zero non-test
callers (see this lote's report). This module is the missing caller: a tiny
authority whose only job is "did this source's content or access change since
last time we looked", answered from one row per `(owner, project, source_ref)`
in the same SQLite store every other `context_engine` module already shares
— not a second store for the same idea (rule 4).

`record_seen()` is the write path a document/code processor calls after it
has a fresh content hash in hand; it returns whether that hash differs from
the one on file and, if so, invalidates the working-set entries that hash
made stale by firing the same `source_version_changed` event
`context_engine/cache.py` now knows how to react to. `revoke()` is the same
invalidation for "this project no longer shares that source" — it does not
delete the row (the source file itself is never touched by a cache module),
it only marks the access grant gone so `is_accessible()` can say so and the
next `record_seen()`/compile sees a scope with nothing left to serve for it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.context_engine import store
from src.context_engine.cache import on_event

logger = logging.getLogger(__name__)

store.register_schema("context_source_versions", (
    """
    CREATE TABLE IF NOT EXISTS source_revisions (
        owner       TEXT NOT NULL DEFAULT '',
        project_id  TEXT NOT NULL DEFAULT '',
        source_ref  TEXT NOT NULL,
        revision    TEXT NOT NULL DEFAULT '',
        revoked     INTEGER NOT NULL DEFAULT 0,
        updated_at  TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, project_id, source_ref)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_source_revisions_scope "
    "ON source_revisions(owner, project_id)",
))


def _norm(value: Any, limit: int = 512) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _row(owner: str, project_id: str, source_ref: str) -> Optional[Dict[str, Any]]:
    try:
        with store.db() as conn:
            row = conn.execute(
                "SELECT revision, revoked FROM source_revisions "
                "WHERE owner = ? AND project_id = ? AND source_ref = ?",
                (owner, project_id, source_ref)).fetchone()
    except store.ContextStoreError:
        logger.warning("context_source_versions: store unavailable for %s", source_ref)
        return None
    if row is None:
        return None
    return {"revision": row["revision"], "revoked": bool(row["revoked"])}


def record_seen(source_ref: str, revision: str, *, owner: str = "",
                project_id: str = "") -> Dict[str, Any]:
    """A processor calls this with the content hash/mtime it just read.

    Returns ``{"changed": bool, "previous_revision": str}``. ``changed`` is
    False the first time a ref is seen — there is nothing stale to invalidate
    yet, only a baseline being recorded — and True whenever the stored
    revision does not match what was just read, at which point this also
    fires ``source_version_changed`` so any packet, candidate list or L1
    response built from the old bytes stops being served from cache. Never
    raises: a store outage degrades to "always changed" (never over-trusts a
    cache it could not consult) rather than taking down the caller.
    """
    ref = _norm(source_ref)
    rev = _norm(revision, limit=256)
    own = _norm(owner, limit=256)
    proj = _norm(project_id, limit=200)
    if not ref:
        return {"changed": False, "previous_revision": ""}

    previous = _row(own, proj, ref)
    if previous is None:
        # Store unavailable is indistinguishable here from "never seen" —
        # `_row` already logged the distinction. Either way, record a
        # baseline optimistically; a store that cannot be read now cannot be
        # written now either, and `store.db()` below will say so.
        changed = False
        previous_revision = ""
    else:
        previous_revision = previous["revision"]
        changed = (previous_revision != rev) or previous["revoked"]

    try:
        with store.db() as conn:
            conn.execute(
                "INSERT INTO source_revisions (owner, project_id, source_ref, "
                "revision, revoked, updated_at) VALUES (?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(owner, project_id, source_ref) DO UPDATE SET "
                "revision = excluded.revision, revoked = 0, "
                "updated_at = excluded.updated_at",
                (own, proj, ref, rev, store.now_iso()))
    except store.ContextStoreError as exc:
        logger.warning("context_source_versions: could not record %s: %s", ref, exc)
        return {"changed": True, "previous_revision": previous_revision}

    if changed and previous is not None:
        invalidate(ref, owner=own, project_id=proj)
    return {"changed": bool(changed and previous is not None),
            "previous_revision": previous_revision}


def revoke(source_ref: str, *, owner: str = "", project_id: str = "") -> bool:
    """Mark a source's access grant gone (a project stopped sharing a file,
    a permission was pulled) and invalidate whatever cache entries depended
    on it. Idempotent — revoking an already-revoked or unknown ref is not an
    error, since the end state either way is "nothing is served for it"."""
    ref = _norm(source_ref)
    own = _norm(owner, limit=256)
    proj = _norm(project_id, limit=200)
    if not ref:
        return False
    try:
        with store.db() as conn:
            conn.execute(
                "INSERT INTO source_revisions (owner, project_id, source_ref, "
                "revision, revoked, updated_at) VALUES (?, ?, ?, '', 1, ?) "
                "ON CONFLICT(owner, project_id, source_ref) DO UPDATE SET "
                "revoked = 1, updated_at = excluded.updated_at",
                (own, proj, ref, store.now_iso()))
    except store.ContextStoreError as exc:
        logger.warning("context_source_versions: could not revoke %s: %s", ref, exc)
        return False
    invalidate(ref, owner=own, project_id=proj)
    return True


def is_accessible(source_ref: str, *, owner: str = "", project_id: str = "") -> bool:
    """False only for a ref this module has actually seen revoked. A ref it
    has never heard of is not thereby forbidden — this store is not the
    authority on access, only on cache staleness; absence of a row means
    "no opinion", not "denied"."""
    row = _row(_norm(owner, limit=256), _norm(project_id, limit=200), _norm(source_ref))
    return not (row is not None and row["revoked"])


def invalidate(source_ref: str, *, owner: str = "", project_id: str = "") -> int:
    """Fire the cache event directly, without touching the revision row —
    for a caller that already knows a source changed (e.g. a batch reindex)
    and wants the same invalidation `record_seen`/`revoke` trigger without a
    second round-trip to read back what it just wrote."""
    return on_event("source_version_changed", {
        "owner": owner, "project_id": project_id, "source_ref": source_ref,
    })


__all__ = ["record_seen", "revoke", "is_accessible", "invalidate"]

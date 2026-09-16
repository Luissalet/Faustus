"""Undo/redo as new revisions — WP12.

"Undo" never rewrites or deletes history: it looks up an earlier
revision's CONTENT (``DocumentStore.get_revision_snapshot``, WP12's one
small additive read method on the WP02 store — see that module) and
applies it as a brand new revision through the normal
``apply_command``/``command_id``/``expected_revision`` path. The append-only
``creator_document_revisions``/``creator_document_commands`` log is
untouched; "redo" is nothing special, it is just another ``undo_to`` call
pointed at a later revision.

The ``undo_to`` op itself is registered in :data:`OPS` like any other typed
op, so it goes through the SAME dispatch, content validation and CAS as
every other op in this package — there is no special-cased "restore" path
in ``store.py``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from ..errors import DocumentNotFound, InvalidOperation

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from ..store import DocumentStore


def _handle_undo_to(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    content = op.get("content")
    if not isinstance(content, dict):
        raise InvalidOperation(
            "undo_to requires an object 'content' — build this op with "
            "undo.undo_to(store, ...), never by hand"
        )
    target_revision = op.get("target_revision")
    if not isinstance(target_revision, int) or isinstance(target_revision, bool) or target_revision < 1:
        raise InvalidOperation("undo_to requires a positive integer 'target_revision'")
    current_revision = doc.get("revision")
    if isinstance(current_revision, int) and target_revision >= current_revision:
        raise InvalidOperation(
            f"undo_to target_revision ({target_revision}) must be earlier than the "
            f"current revision ({current_revision})"
        )
    return content


def undo_to(
    store: "DocumentStore",
    owner: str,
    doc_id: str,
    target_revision: int,
    command_id: str,
    *,
    expected_revision: int,
) -> Dict[str, Any]:
    """Creates a NEW revision whose content equals ``target_revision``'s.

    Returns the same shape as ``DocumentStore.apply_command``:
    ``{"doc": CreatorDocument, "applied": bool, "deduped": bool}``. Raises
    :class:`DocumentNotFound` if the document (or that revision of it,
    scoped to the same owner) does not exist, or
    :class:`~src.creator.errors.RevisionConflict`/``InvalidOperation`` from
    the underlying ``apply_command`` call as usual.

    "Redo" is the same call with a later ``target_revision`` — this
    function does not distinguish the two directions.
    """
    snapshot = store.get_revision_snapshot(owner, doc_id, target_revision)
    if snapshot is None:
        raise DocumentNotFound(doc_id)
    op = {
        "type": "undo_to",
        "target_revision": int(target_revision),
        "content": snapshot["content"],
    }
    return store.apply_command(owner, doc_id, command_id, expected_revision, op)


OPS = {
    "undo_to": _handle_undo_to,
}

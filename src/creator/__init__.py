"""Creator domain package (WP02): typed revisioned documents + profile.

Interface the rest of the Creator plan (WP03+) assumes, unchanged:

    DocumentStore.get(owner, doc_id) -> CreatorDocument | None
    DocumentStore.create(owner, project_id, kind, content) -> CreatorDocument
    DocumentStore.apply_command(owner, doc_id, command_id, expected_revision, op)
        -> {"doc": CreatorDocument, "applied": bool, "deduped": bool}
        (stale expected_revision -> RevisionConflict(current_revision))
    DocumentStore.history(owner, doc_id) -> list[dict]

    profile.get_profile(project_store, owner, project_id) -> dict | None
    profile.set_profile(project_store, owner, project_id, patch) -> dict | None
"""
from .documents import CreatorDocument, DOCUMENT_KINDS, DOCUMENT_STATES, SCHEMA_VERSION
from .errors import (
    CreatorError,
    DocumentNotFound,
    InvalidDocument,
    InvalidOperation,
    RevisionConflict,
)
from .store import DocumentStore, get_store

__all__ = [
    "CreatorDocument",
    "DOCUMENT_KINDS",
    "DOCUMENT_STATES",
    "SCHEMA_VERSION",
    "CreatorError",
    "DocumentNotFound",
    "InvalidDocument",
    "InvalidOperation",
    "RevisionConflict",
    "DocumentStore",
    "get_store",
]

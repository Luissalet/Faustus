"""edit_journal.py — EDIT-02: journal and compensation for a multi-file batch.

Faustus's filesystem tools do not have real cross-file atomicity: there is no
OS-level transaction spanning several `write()` calls. `ApplyPatchTool`
already validates and computes every file's new content before writing
anything (see `src/agent_tools/filesystem_tools.py`), but the write loop
itself used to have no memory of what it had already done — a disk failure
partway through a multi-file patch left the files written so far written, the
files not yet reached untouched, and nothing recording which was which
(QA-17, docs/spec/v2/acceptance_scenarios.json).

This module is that memory. `EditJournal` records the intent of a batch
(every target path and its pre-batch bytes) BEFORE any write runs, so a
failure partway through has something to compensate FROM. `apply_batch()`
drives one batch end to end: apply each op in order, and on the first
failure, write every already-applied file back to its pre-batch bytes (or
delete it, if it did not exist before the batch). That is compensation, not a
true rollback — each compensating write is itself a plain filesystem write
that can also fail — so `rolled_back` is `True` only when every compensation
actually succeeded, and the journal's `applied` list is the honest record of
what landed either way (EDIT-02's required shape: `applied`, `failed_at`,
`rolled_back`).

`changeset_from_receipt()` turns a journal's receipt into the same `ChangeSet`
shape `src/changeset_store.py` persists at turn/dispatch completion — reusing
that authority's vocabulary rather than inventing a second one (COMUN.md rule
4) — without writing to its store directly: that store is a completion hook
for a whole turn or dispatch job, not an intermediate step inside one, so
persisting this receipt is left to whichever of those wraps the batch.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def revision_of(data: Optional[bytes]) -> Optional[str]:
    """Same anchor format as EDIT-01 (`sha256:<hex>`, spec §34.2). `None` in
    means the path did not exist — and `None` comes back, not a hash of
    nothing, so "missing" and "empty file" stay distinguishable."""
    if data is None:
        return None
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class JournalEntry:
    path: str
    pre_bytes: Optional[bytes]       # None = the path did not exist before the batch
    pre_revision: Optional[str]
    post_revision: Optional[str] = None
    applied: bool = False
    error: Optional[str] = None


@dataclass
class EditJournal:
    """The diary for one multi-file batch: what was intended, what landed,
    and — on failure — what was put back."""

    entries: List[JournalEntry] = field(default_factory=list)
    failed_at: Optional[str] = None
    failure: Optional[str] = None
    rolled_back: Optional[bool] = None   # None until a failure happens

    def record_intent(self, paths: List[str], read_current: Callable[[str], Optional[bytes]]) -> None:
        """Snapshot every target BEFORE any write. This is the entry the rest
        of the batch is checked and, if needed, restored against — it must
        happen while the workspace is still exactly as the caller found it."""
        self.entries = []
        for p in paths:
            data = read_current(p)
            self.entries.append(JournalEntry(path=p, pre_bytes=data, pre_revision=revision_of(data)))

    def _entry(self, path: str) -> Optional[JournalEntry]:
        return next((e for e in self.entries if e.path == path), None)

    def mark_applied(self, path: str, post_bytes: Optional[bytes]) -> None:
        e = self._entry(path)
        if e is not None:
            e.applied = True
            e.error = None
            e.post_revision = revision_of(post_bytes)

    def mark_failed(self, path: str, error: str) -> None:
        self.failed_at = path
        self.failure = error
        e = self._entry(path)
        if e is not None:
            e.error = error

    def compensate(self, write_back: Callable[[str, Optional[bytes]], None]) -> bool:
        """Undo every entry marked `applied`, most-recently-applied first, by
        writing back its pre-batch bytes (`write_back(path, None)` for a path
        that did not exist before — the caller deletes it). Returns whether
        EVERY compensation succeeded; a partial failure leaves `rolled_back =
        False` and the entries still marked `applied` name exactly which
        files are left in whatever state that failed write put them, instead
        of the journal silently claiming a clean undo that did not happen."""
        ok = True
        for e in reversed(self.entries):
            if not e.applied:
                continue
            try:
                write_back(e.path, e.pre_bytes)
                e.applied = False
                e.post_revision = e.pre_revision
            except OSError as exc:
                logger.warning("[edit_journal] compensation for %s failed: %s", e.path, exc)
                ok = False
        self.rolled_back = ok
        return ok

    def receipt(self) -> Dict[str, Any]:
        """EDIT-02's required shape, plus enough per-file detail to build a
        ChangeSet from it (see `changeset_from_receipt`)."""
        return {
            "applied": [e.path for e in self.entries if e.applied],
            "failed_at": self.failed_at,
            "failure": self.failure,
            "rolled_back": self.rolled_back,
            "files": [
                {"path": e.path, "pre_revision": e.pre_revision,
                 "post_revision": e.post_revision, "applied": e.applied,
                 "error": e.error}
                for e in self.entries
            ],
        }


def apply_batch(
    ops: List[Dict[str, Any]],
    *,
    read_bytes: Callable[[str], Optional[bytes]],
    write_bytes: Callable[[str, bytes], None],
    delete_path: Callable[[str], None],
    apply_op: Callable[[str, Dict[str, Any], Optional[bytes]], Optional[bytes]],
) -> Dict[str, Any]:
    """Apply `ops` (each at least `{"path": ...}`) as one journaled batch.

    `apply_op(path, op, pre_bytes)` performs and WRITES one op's change and
    returns the new bytes (or `None` for a delete); `read_bytes`/`write_bytes`
    /`delete_path` are used only for the journal's own snapshot-and-restore,
    never for the forward apply. The first `OSError` from `apply_op` stops
    the batch and compensates every op already applied, in reverse order.
    """
    paths = [str(op["path"]) for op in ops]
    journal = EditJournal()
    journal.record_intent(paths, read_bytes)
    for op in ops:
        path = str(op["path"])
        entry = journal._entry(path)
        pre = entry.pre_bytes if entry is not None else None
        try:
            post = apply_op(path, op, pre)
        except OSError as exc:
            journal.mark_failed(path, f"{type(exc).__name__}: {exc}")

            def _write_back(p: str, data: Optional[bytes]) -> None:
                if data is None:
                    delete_path(p)
                else:
                    write_bytes(p, data)

            journal.compensate(_write_back)
            return journal.receipt()
        journal.mark_applied(path, post)
    return journal.receipt()


def changeset_from_receipt(receipt: Dict[str, Any], *, workspace: str = "", owner: str = "",
                           project_id: str = "", run_id: str = "", title: str = ""):
    """The journal's receipt, reshaped into a `ChangeSet` (`src/changesets.py`
    — the same authority `changeset_store` persists) so a caller that wraps a
    batch in a turn or dispatch job can hand this straight to
    `changeset_store.record_turn`/`record_dispatch` instead of building its
    own evidence shape."""
    from src.changesets import build as _build

    applied = list(receipt.get("applied") or [])
    changes = {"source": "none", "modified": applied}
    claims = [{"path": p, "kind": "modified"} for p in applied]
    return _build(intent="fix", workspace=workspace, changes=changes, verification={},
                 claims=claims, title=title or "multi-file batch",
                 owner=owner, project_id=project_id, run_id=run_id)

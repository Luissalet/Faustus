"""
writing_project_adapter.py — WRITE-05: integration without duplication.

Import/export for a project that lives in an external writing application
(Writer's Hoard, or anything shaped the same way — a manifest of chapters,
entities and resources), without requiring that project to be a git
repository (the acceptance line is explicit about that) and without a text
ever living in two places at once: an item this module tracks is a
REFERENCE (id, kind, a content hash, when it last changed) — never a second
copy of the content itself. Moving actual bytes for an item `plan_sync`
names is the caller's job (the real "adaptador de proyecto" a route would
wire up, ajeno to this batch); this module only ever decides WHICH items
need that and flags what it cannot decide safely.

Conflict detection reuses the exact idea CONN-04 already established for
CalDAV (`src/caldav_writeback.py`, this same batch): a stored "last known"
hash per item, compared against BOTH sides' CURRENT hash before anything is
proposed to move — an item changed on both sides since the last sync is a
conflict, reported with both hashes, never silently resolved by import
always winning or export always winning.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ITEM_KINDS = ("chapter", "entity", "resource")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


@dataclass
class ManifestItem:
    id: str
    kind: str
    title: str
    content_hash: str
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "content_hash": self.content_hash, "updated_at": self.updated_at}


@dataclass
class ProjectManifest:
    project_id: str
    items: Dict[str, ManifestItem] = field(default_factory=dict)

    @classmethod
    def from_items(cls, project_id: str, items: List[ManifestItem]) -> "ProjectManifest":
        return cls(project_id=project_id, items={item.id: item for item in items})


@dataclass
class SyncState:
    """The "last known" hash per item id, from the last sync that actually
    resolved it — this module's OWN small store, distinct from both sides'
    content stores (rule 4: a new concept, not a second copy of an existing
    one)."""
    project_id: str
    last_synced_hash: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"project_id": self.project_id, "last_synced_hash": dict(self.last_synced_hash)}


def diff_manifests(local: ProjectManifest, remote: ProjectManifest,
                   state: SyncState) -> Dict[str, List[str]]:
    """Classify every item id present on either side against what was last
    synced. An id with NO prior sync record that differs on both sides is
    classified `conflict`, not resolved by favouring either side — there is
    no basis yet for knowing which one is the intended original."""
    ids = set(local.items) | set(remote.items)
    out: Dict[str, List[str]] = {"local_only": [], "remote_only": [], "unchanged": [],
                                 "local_changed": [], "remote_changed": [], "conflict": []}
    for item_id in sorted(ids):
        local_item = local.items.get(item_id)
        remote_item = remote.items.get(item_id)
        last = state.last_synced_hash.get(item_id)
        if local_item is None:
            out["remote_only"].append(item_id)
        elif remote_item is None:
            out["local_only"].append(item_id)
        elif local_item.content_hash == remote_item.content_hash:
            out["unchanged"].append(item_id)
        else:
            local_changed = last is None or local_item.content_hash != last
            remote_changed = last is None or remote_item.content_hash != last
            if local_changed and remote_changed:
                out["conflict"].append(item_id)
            elif local_changed:
                out["local_changed"].append(item_id)
            else:
                out["remote_changed"].append(item_id)
    return out


def plan_sync(local: ProjectManifest, remote: ProjectManifest,
             state: SyncState) -> Dict[str, Any]:
    """What a bidirectional sync would move, WITHOUT moving anything —
    "mostrar que lado tiene cambios nuevos" made literal. The acceptance
    line's "no sobrescribe ediciones concurrentes" is a property of this
    function's OUTPUT: anything in `conflicts` is absent from both `pull`
    and `push`, so a caller that blindly executes this plan can never
    overwrite a concurrent edit — only a caller that separately, explicitly
    resolves a conflict can."""
    classified = diff_manifests(local, remote, state)
    return {
        "pull": classified["remote_only"] + classified["remote_changed"],
        "push": classified["local_only"] + classified["local_changed"],
        "conflicts": classified["conflict"],
        "unchanged": classified["unchanged"],
    }


def apply_sync_result(state: SyncState, local: ProjectManifest,
                      remote: ProjectManifest, plan: Dict[str, Any]) -> SyncState:
    """After the CALLER has actually moved bytes for every id in
    `plan['pull']`/`plan['push']` (this module never touches item content —
    WRITE-05's own "sin duplicacion": one content owner per item, this
    module only tracks which side changed), record the new last-synced
    hash for everything no longer in conflict. Anything still in
    `plan['conflicts']` is left OUT on purpose: the next `diff_manifests`
    must keep flagging it until a person resolves it, not silently forget
    that it was ever unresolved."""
    updated = dict(state.last_synced_hash)
    for item_id in plan["pull"] + plan["push"] + plan["unchanged"]:
        item = local.items.get(item_id) or remote.items.get(item_id)
        if item is not None:
            updated[item_id] = item.content_hash
    return SyncState(project_id=state.project_id, last_synced_hash=updated)

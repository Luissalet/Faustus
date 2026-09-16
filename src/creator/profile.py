"""CreatorProfile — per-project Creator preferences (WP02, feature UX01).

Stored as a single ``context_item`` on the SAME ``ProjectStore`` row every
project already has (``services/projects.py::ProjectStore``), never a
parallel table (CONTRATO.md rule 1 and WP02's own ficha: "sobre
ProjectStore.context_items, sin tabla paralela"). The link uses
``kind="document"`` (one of the store's existing ``LINK_KINDS``) with a
fixed ``ref_id="creator_profile"`` so there is at most one per project, and
the JSON payload rides in the link's free-text ``summary`` field — the only
field in the link's typed shape wide enough for structured content, and
already used for arbitrary text by the store's own AI-summary callers.

This does not change project identity, ownership rules or ``project_id``:
UX01's acceptance is that Chat/Agent and Creator alternate on the SAME
project. A project the caller does not own resolves to ``None`` here, the
same as a project that does not exist — the caller (route layer) turns
both into a 404, per CONTRATO.md rule 3.

Legacy/untouched projects: reading a project with no profile link returns
the DEFAULT_PROFILE dict without writing anything. Nothing is created until
the first ``set_profile`` call (CONTRATO.md rule 5 / WP02 ficha).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

PROFILE_REF_ID = "creator_profile"
PROFILE_KIND = "document"
PROFILE_SCHEMA_VERSION = 1

# Freeform but versioned. UX01 only requires that Creator can carry its own
# preferences without touching project_id/permissions/Chat/Agent state; the
# concrete keys here are display/tool defaults a later WP (Studio, WP10) is
# free to read and extend additively — never reinterpreted, only migrated by
# bumping schema_version.
DEFAULT_PROFILE: Dict[str, Any] = {
    "schema_version": PROFILE_SCHEMA_VERSION,
    "default_kind": "canvas",
    "preferred_kinds": [],
    "tool_defaults": {},
    "layout": {},
}


def _decode(summary: str) -> Dict[str, Any]:
    try:
        data = json.loads(summary) if summary else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    merged = dict(DEFAULT_PROFILE)
    merged.update(data)
    merged["schema_version"] = PROFILE_SCHEMA_VERSION
    return merged


def get_profile(project_store, owner: Optional[str], project_id: str) -> Optional[Dict[str, Any]]:
    """The project's Creator profile, or ``None`` for an unknown/foreign
    project. Never writes. Always includes every ``DEFAULT_PROFILE`` key so
    callers do not need to know whether a profile link exists yet."""
    project = project_store.get(project_id, owner)
    if project is None:
        return None
    for link in project_store.normalized_links(project):
        if link["kind"] == PROFILE_KIND and link["ref_id"] == PROFILE_REF_ID:
            return _decode(link.get("summary") or "")
    return dict(DEFAULT_PROFILE)


def set_profile(
    project_store, owner: Optional[str], project_id: str, patch: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Merge ``patch`` onto the current profile and persist it as the
    project's single ``creator_profile`` context item. Returns the new
    profile, or ``None`` for an unknown/foreign project (nothing written).
    """
    project = project_store.get(project_id, owner)
    if project is None:
        return None
    current = get_profile(project_store, owner, project_id) or dict(DEFAULT_PROFILE)
    merged = dict(current)
    for key, value in (patch or {}).items():
        if key == "schema_version":
            continue
        merged[key] = value
    merged["schema_version"] = PROFILE_SCHEMA_VERSION
    payload = json.dumps(merged, ensure_ascii=False)

    existing_link_id = None
    for link in project_store.normalized_links(project):
        if link["kind"] == PROFILE_KIND and link["ref_id"] == PROFILE_REF_ID:
            existing_link_id = link["id"]
            break

    if existing_link_id:
        project_store.patch_link(
            project_id, existing_link_id, {"summary": payload}, owner=owner,
        )
    else:
        project_store.upsert_link(
            project_id,
            {
                "kind": PROFILE_KIND,
                "ref_id": PROFILE_REF_ID,
                "label": "Creator profile",
                "role": "reference",
                "retrieval_policy": "disabled",
                "summary": payload,
            },
            owner=owner,
        )
    return merged

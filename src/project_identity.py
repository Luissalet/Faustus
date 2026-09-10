"""Project folder identity — a stable id plus a marker that survives a disk
move (IDX-01, FAUSTUS).

``services/projects.py::ProjectStore`` already gives a project an ``id`` that
is independent of its ``workspace`` path — renaming or moving the folder and
pointing ``workspace`` at the new location keeps every chat/memory/relation
keyed by that id intact (see
``tests/test_project_identity.py::test_project_id_survives_renaming_the_folder``
and ``tests/qa/test_qa_48_migracion_de_carpeta.py``). What was missing per
``docs/spec/v2/MAPA_REUTILIZACION.md`` (IDX-01 row) is:

* a way for the FOLDER ITSELF to corroborate that identity — a
  ``.faustus/project.json`` marker carrying the same ``project_id`` plus a
  structure fingerprint, so a copy or a backup of the folder can be told
  apart from an unrelated one even without consulting the database; and
* an explicit entry point for "I moved this to `new_path`"
  (:func:`relocate`) that resolves an absent path by SAYING SO — never by
  silently accepting a path that is not there, and never by inventing a
  project.

This module reuses ``services.projects.ProjectStore`` as the single
authority for the id; it keeps no second project registry of its own (rule:
reuse existing authorities).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MARKER_DIRNAME = ".faustus"
MARKER_FILENAME = "project.json"
MARKER_SCHEMA_VERSION = 1

# Noise a structure hash must ignore: it fingerprints the PROJECT, not the
# build/version-control churn inside it. A fresh `node_modules` install or a
# new `.git` commit must not look like "a different folder".
IGNORED_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    MARKER_DIRNAME, ".odysseus", ".pytest_cache", ".mypy_cache", ".tox",
    ".idea", ".vscode",
})
# A bounded walk: a relocate on a huge tree costs a capped scan, not a full
# index. Past this many files the hash still runs, it just stops widening —
# a large project's structure is still meaningfully fingerprinted by its
# first N files in sorted order.
MAX_HASH_ENTRIES = 20000


class ProjectIdentityError(ValueError):
    """An explicit, described failure to resolve a folder's identity —
    never a silent fallback. Routes should map this to 400/409."""


def marker_path(workspace: str) -> str:
    return os.path.join(workspace, MARKER_DIRNAME, MARKER_FILENAME)


def compute_structure_hash(workspace: str, *, max_entries: int = MAX_HASH_ENTRIES) -> str:
    """SHA-256 over the sorted list of relative file paths under ``workspace``.

    Content is deliberately NOT hashed — a project's files change on every
    edit, its shape does not — and bare directories are not listed on their
    own (an empty one carries no signal). Returns ``""`` for a path that is
    not a readable directory, so callers can treat "no hash" as "no folder
    there", not as a hash collision.
    """
    if not workspace or not os.path.isdir(workspace):
        return ""
    entries = []
    try:
        for root, dirs, files in os.walk(workspace):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIR_NAMES)
            for name in sorted(files):
                rel = os.path.relpath(os.path.join(root, name), workspace)
                entries.append(rel.replace(os.sep, "/"))
                if len(entries) >= max_entries:
                    break
            if len(entries) >= max_entries:
                break
    except OSError as exc:
        logger.warning("could not walk %s for a structure hash: %s", workspace, exc)
        return ""
    digest = hashlib.sha256("\n".join(sorted(entries)).encode("utf-8", "replace"))
    return digest.hexdigest()


def read_marker(workspace: str) -> Optional[Dict[str, Any]]:
    """The marker at ``workspace``, or None if absent/unreadable. Never
    raises — a marker is corroborating evidence, not the source of truth."""
    path = marker_path(workspace)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("project marker at %s is unreadable (%s); ignored", path, exc)
        return None
    return data if isinstance(data, dict) else None


def write_marker(workspace: str, project_id: str, *, refresh_hash: bool = True,
                 force: bool = False) -> Dict[str, Any]:
    """Create or refresh ``.faustus/project.json``.

    An existing marker for a DIFFERENT project_id is never silently
    overwritten — that would misattribute a folder someone else already
    marked — UNLESS ``force`` is set, for the one caller allowed to
    reassign a folder on purpose: :func:`relocate`, acting on an explicit
    user instruction. A write failure (read-only mount, full disk) degrades
    to a logged warning with ``write_failed`` on the returned dict rather
    than raising: the database update via ``ProjectStore`` is the real
    authority, and it already happened by the time this is called from
    :func:`relocate`.
    """
    existing = read_marker(workspace)
    if (existing and not force
            and str(existing.get("project_id") or "") not in ("", str(project_id))):
        raise ProjectIdentityError(
            f"'{workspace}' already carries a marker for project "
            f"{existing.get('project_id')!r}, not {project_id!r}"
        )
    marker: Dict[str, Any] = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "project_id": str(project_id),
        "structure_hash": (compute_structure_hash(workspace) if refresh_hash
                           else str((existing or {}).get("structure_hash") or "")),
    }
    directory = os.path.join(workspace, MARKER_DIRNAME)
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = marker_path(workspace) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(marker, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, marker_path(workspace))
    except OSError as exc:
        logger.warning("could not write project marker at %s: %s", workspace, exc)
        marker = dict(marker, write_failed=str(exc))
    return marker


def verify_marker(workspace: str, project_id: str) -> Dict[str, Any]:
    """Does the folder AT ``workspace`` agree it is ``project_id``?

    Three independent facts, so a caller can tell "never marked" apart from
    "marked as a DIFFERENT project" apart from "marked correctly but the
    tree changed since" (a normal refactor, not proof of a wrong folder):
    ``present``, ``project_id_matches`` and ``structure_hash_matches`` (None
    when there is nothing stored to compare against).
    """
    marker = read_marker(workspace)
    if marker is None:
        return {"present": False, "project_id_matches": None, "structure_hash_matches": None}
    matches_id = str(marker.get("project_id") or "") == str(project_id)
    stored_hash = str(marker.get("structure_hash") or "")
    current_hash = compute_structure_hash(workspace)
    return {
        "present": True,
        "project_id_matches": matches_id,
        "structure_hash_matches": (stored_hash == current_hash) if stored_hash else None,
    }


def ensure_marker(project: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Backfill a marker for a project created before this module existed.

    Additive and quiet: a project with no workspace, or whose workspace does
    not (yet) exist on disk, is skipped rather than errored — the same
    posture as ``ProjectStore.scaffold_memory``'s own OSError handling.
    """
    workspace = str((project or {}).get("workspace") or "")
    project_id = str((project or {}).get("id") or "")
    if not workspace or not project_id or not os.path.isdir(workspace):
        return None
    if read_marker(workspace) is not None:
        return None
    try:
        return write_marker(workspace, project_id)
    except ProjectIdentityError:
        return None


def relocate(project_id: str, new_path: str, *, owner: Optional[str] = None,
            store: Optional[Any] = None) -> Dict[str, Any]:
    """Point ``project_id`` at ``new_path`` on disk, explicitly.

    The single authority for identity stays
    ``services.projects.ProjectStore`` — ``update()`` already keeps every
    memory and relation keyed by ``project_id`` intact across a ``workspace``
    change (``tests/qa/test_qa_48_migracion_de_carpeta.py``). This function
    adds the two things a raw ``update()`` call does not:

    * it REFUSES a path that is not there. An absent path is resolved by
      raising :class:`ProjectIdentityError`, never by pointing the project at
      nothing and hoping a later read notices.
    * it writes/refreshes the ``.faustus/project.json`` marker at the new
      location, so the folder itself corroborates the move from then on.

    Returns ``{"project", "marker", "old_workspace", "old_path_missing",
    "marker_conflict"}``. ``old_path_missing`` is informational, not an
    error — that is usually exactly WHY relocate is being called.
    ``marker_conflict`` is true when the destination already carries another
    project's marker (a folder someone reused), also informational: the
    caller decided to point here anyway, and gets a flag to surface that
    instead of a silent overwrite.
    """
    from services import projects as projects_mod

    st = store or projects_mod.get_store()
    project = st.get(project_id, owner=owner)
    if not project:
        raise ProjectIdentityError(f"no project with id {project_id!r}")

    candidate = os.path.expanduser(str(new_path or "").strip())
    if not candidate:
        raise ProjectIdentityError("new_path must not be empty")
    if not os.path.isdir(candidate):
        # The explicit resolution IDX-01 asks for: an absent path is never
        # silently accepted as the project's new home, and nothing about the
        # project changes when it is refused.
        raise ProjectIdentityError(
            f"'{new_path}' does not exist or is not a folder — the project "
            "was NOT relocated"
        )

    old_workspace = str(project.get("workspace") or "")
    # Read BEFORE `ProjectStore.update()` runs: `update()` calls
    # `services.objectives.preserve_for_rebinding`, which takes a lock file
    # under `<old_workspace>/.odysseus/` and — as a side effect of acquiring
    # it — recreates that directory tree even when the old workspace is
    # otherwise gone. Checking afterwards would report "present" for a path
    # that was actually missing at the moment relocate was called.
    old_path_missing = bool(old_workspace) and not os.path.isdir(old_workspace)
    conflict_check = verify_marker(candidate, project_id)

    updated = st.update(project_id, {"workspace": candidate}, owner=owner)
    if updated is None:
        raise ProjectIdentityError(f"project {project_id!r} could not be updated")

    # `force=True`: relocate IS the explicit user action allowed to reassign
    # a folder's marker, even one another project left behind (a reused or
    # copied folder) — `marker_conflict` below tells the caller it happened.
    marker = write_marker(candidate, project_id, force=True)
    return {
        "project": updated,
        "marker": marker,
        "old_workspace": old_workspace,
        "old_path_missing": old_path_missing,
        "marker_conflict": bool(conflict_check.get("present")
                                and conflict_check.get("project_id_matches") is False),
    }

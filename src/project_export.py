"""project_export.py — OPS-04: portability of a project across disks/machines.

Reuses, does not reimplement:

  * ``services.projects.get_store()`` — the one project registry (id, name,
    workspace, chat folder). This module never keeps a second one;
  * ``src.project_identity`` — the ``.faustus/project.json`` marker (IDX-01)
    is how an imported folder proves, on the other end, that it really is
    the project the manifest says it is;
  * the DB's ``Session``/``ChatMessage`` rows, scoped by ``Session.
    project_id`` — chats are not files, exporting them means querying the
    same tables the chat UI reads, not inventing a parallel chat store;
  * ``<workspace>/.odysseus/`` — per ``services/projects.py``'s own module
    docstring, "project memory is Markdown on disk" there already. Exporting
    it is copying files that already exist at a stable, workspace-relative
    location — not a new memory format.

Every path inside a bundle is relative to the project's workspace — never an
absolute path from the machine that exported it — so a bundle moved to
another disk, another machine, or another username still applies cleanly
(OPS-04's acceptance criterion: "mover un proyecto a otro disco mantiene
identidad y enlaces").

Secrets are excluded by default: any file under ``.odysseus/`` whose name
matches a common credential pattern (``.env``, ``*.key``, ``*.pem``,
``*credentials*``, ``*token*``, ``*secret*``) is skipped and named in the
manifest's ``excluded_for_secrets`` list — visible, never silently dropped.

Import is preview-first (``dry_run=True`` by default): nothing is written
until a caller explicitly asks. Every entry the manifest lists but the
bundle does not actually contain is reported in ``lost_references`` — shown,
never silently discarded (the acceptance criterion this answers to).

NOT covered by this module, honestly: "skills" (no per-project skill
scoping exists anywhere in the codebase today to export — SKILLS_DIR is
global, not project-keyed) and "artefactos" beyond a reference list (a full
binary copy would need ``include_artifact_files=True`` and is opt-in,
since artifact stores can be large and are already content-addressed and
re-fetchable). See this lot's report for the precise gap.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import time
import uuid
import zipfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BUNDLE_SCHEMA_VERSION = 1

# Filenames under .odysseus/ that never leave the machine by default, even
# when the rest of the memory tree is exported. Matched case-insensitively
# against the basename only — a real secret's path, not its content, is
# what this module can see without reading (and possibly leaking) it.
_SECRET_NAME_PATTERNS = (
    ".env", ".env.*", "*.key", "*.pem", "*credentials*", "*secret*", "*token*",
)


def _is_secret_name(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in _SECRET_NAME_PATTERNS)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relpath(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _iter_memory_files(workspace: str):
    """Files under <workspace>/.odysseus/, relative paths, secret-named ones
    filtered out before they are ever read."""
    from services.projects import MEMORY_DIRNAME
    mem_root = os.path.join(workspace, MEMORY_DIRNAME)
    if not os.path.isdir(mem_root):
        return
    for root, dirs, files in os.walk(mem_root):
        for name in sorted(files):
            if _is_secret_name(name):
                continue
            full = os.path.join(root, name)
            yield full, _relpath(workspace, full)


def _project_or_raise(project_id: str, *, owner: Optional[str] = None, store=None) -> Dict[str, Any]:
    from services import projects as projects_mod
    st = store or projects_mod.get_store()
    project = st.get(project_id, owner=owner)
    if not project:
        raise ValueError(f"no project with id {project_id!r}")
    return project


def _export_chats(project_id: str) -> List[Dict[str, Any]]:
    """Every session bound to this project (Session.project_id), each with
    its messages, in a plain JSON shape — never re-derived from a parser,
    read straight off the same columns the chat UI does."""
    from core.database import SessionLocal, Session as DbSession, ChatMessage as DbChatMessage
    db = SessionLocal()
    try:
        sessions = db.query(DbSession).filter(DbSession.project_id == project_id).all()
        out = []
        for s in sessions:
            messages = (
                db.query(DbChatMessage)
                .filter(DbChatMessage.session_id == s.id)
                .order_by(DbChatMessage.timestamp)
                .all()
            )
            out.append({
                "id": s.id, "name": s.name, "model": s.model, "folder": s.folder,
                "archived": bool(s.archived), "created_at": str(s.created_at) if s.created_at else None,
                "messages": [
                    {"id": m.id, "role": m.role, "content": m.content, "metadata": m.meta_data,
                     "timestamp": str(m.timestamp) if m.timestamp else None}
                    for m in messages
                ],
            })
        return out
    finally:
        db.close()


def export_project(
    project_id: str, *, owner: Optional[str] = None, dest_dir: Optional[str] = None,
    include_chats: bool = True, include_memory: bool = True,
    include_artifact_refs: bool = True,
) -> str:
    """Build a portable .zip for `project_id` and return its path. Every
    path recorded inside is workspace-relative; nothing absolute from this
    machine is written into the bundle."""
    project = _project_or_raise(project_id, owner=owner)
    workspace = str(project.get("workspace") or "")

    from src import project_identity
    marker = project_identity.read_marker(workspace) if workspace else None

    manifest: Dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "exported_at": time.time(),
        "project_id": project_id,
        "project_name": project.get("name"),
        "marker": marker,
        "files": [],              # [{"path", "sha256", "bytes"}] — memory files, relative
        "excluded_for_secrets": [],
        "chats": [],               # session ids included, for a quick preview without unzipping
        "artifact_refs": [],
        "not_covered": ["skills"],  # honest, see module docstring
    }

    dest_dir = dest_dir or os.path.join(_data_dir(), "project_exports")
    os.makedirs(dest_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bundle_path = os.path.join(dest_dir, f"project_{project_id}_{stamp}.zip")

    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if include_memory and workspace:
            for full, rel in _iter_memory_files(workspace):
                with open(full, "rb") as f:
                    data = f.read()
                digest = _sha256_bytes(data)
                zf.writestr(f"files/{rel}", data)
                manifest["files"].append({"path": rel, "sha256": digest, "bytes": len(data)})
            # Secret-named files ARE walked (for the manifest note) but their
            # bytes are never read or written into the bundle.
            from services.projects import MEMORY_DIRNAME
            mem_root = os.path.join(workspace, MEMORY_DIRNAME)
            if os.path.isdir(mem_root):
                for root, _dirs, files in os.walk(mem_root):
                    for name in files:
                        if _is_secret_name(name):
                            manifest["excluded_for_secrets"].append(
                                _relpath(workspace, os.path.join(root, name)))

        if include_chats:
            chats = _export_chats(project_id)
            zf.writestr("chats.json", json.dumps(chats, ensure_ascii=False, indent=2))
            manifest["chats"] = [{"id": c["id"], "name": c["name"]} for c in chats]

        if include_artifact_refs:
            manifest["artifact_refs"] = _artifact_refs_for_project(project_id)

        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return bundle_path


def _artifact_refs_for_project(project_id: str) -> List[Dict[str, Any]]:
    """Reference-only: id + hash, no bytes. A full copy is
    `include_artifact_files=True` in a future caller (not needed for the
    common case — artifacts are already content-addressed and re-fetchable
    from src.artifact_store by hash)."""
    try:
        from core.database import SessionLocal, ArtifactRow
        db = SessionLocal()
        try:
            rows = db.query(ArtifactRow).filter(ArtifactRow.project_id == project_id).all() \
                if hasattr(ArtifactRow, "project_id") else []
            return [{"id": r.id, "filename": getattr(r, "filename", None)} for r in rows]
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - artifacts are a bonus, not load-bearing here
        logger.debug("project_export: artifact refs unavailable: %s", e)
        return []


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return DATA_DIR
    except Exception:  # pragma: no cover
        return os.path.join(os.getcwd(), "data")


# ── import: preview-first, lost references named not dropped ───────────────


def preview_import(bundle_path: str) -> Dict[str, Any]:
    """Read a bundle's manifest and check it against what the zip ACTUALLY
    contains, without writing anything. `lost_references` names every file
    the manifest lists whose bytes are not really in the archive (a
    truncated copy, a hand-edited manifest) — shown, never silently
    skipped."""
    with zipfile.ZipFile(bundle_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            raise ValueError("not a project export bundle: manifest.json missing")
        names = set(zf.namelist())
        lost: List[str] = []
        for entry in manifest.get("files") or []:
            zip_name = f"files/{entry['path']}"
            if zip_name not in names:
                lost.append(entry["path"])
                continue
            data = zf.read(zip_name)
            if _sha256_bytes(data) != entry.get("sha256"):
                lost.append(entry["path"])
        chats_present = "chats.json" in names
        return {
            "project_id": manifest.get("project_id"),
            "project_name": manifest.get("project_name"),
            "file_count": len(manifest.get("files") or []),
            "chat_count": len(manifest.get("chats") or []),
            "chats_payload_present": chats_present,
            "excluded_for_secrets": manifest.get("excluded_for_secrets") or [],
            "not_covered": manifest.get("not_covered") or [],
            "lost_references": lost,
        }


def import_project(
    bundle_path: str, *, target_workspace: str, owner: Optional[str] = None,
    dry_run: bool = True, project_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a bundle at `target_workspace`. `dry_run=True` (default) returns
    the same preview `preview_import` does, plus what WOULD happen (new
    project vs. relocate-onto-existing, via `src.project_identity`), and
    changes nothing. `dry_run=False` actually writes.

    Never silently claims `target_workspace` for a different project than
    a marker already there names — that refusal is `project_identity`'s
    job, reused here rather than re-decided."""
    from src import project_identity
    from services import projects as projects_mod

    report = preview_import(bundle_path)
    with zipfile.ZipFile(bundle_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json"))

        marker_check = (project_identity.verify_marker(target_workspace, manifest.get("project_id"))
                        if os.path.isdir(target_workspace) else
                        {"present": False, "project_id_matches": None, "structure_hash_matches": None})
        report["target_marker"] = marker_check
        report["would_conflict"] = bool(
            marker_check.get("present") and marker_check.get("project_id_matches") is False
        )
        if dry_run:
            report["dry_run"] = True
            return report
        if report["would_conflict"]:
            raise ValueError(
                f"{target_workspace!r} is already marked for a different project "
                f"— refusing to import without an explicit relocate first"
            )

        os.makedirs(target_workspace, exist_ok=True)
        st = projects_mod.get_store()
        original_project_id = str(manifest.get("project_id") or "")
        existing = st.get(original_project_id, owner=owner) if original_project_id else None
        if existing is not None:
            # Same id already registered here (re-importing onto the same
            # store, or the id was pre-created) — identity preserved exactly.
            project_id = original_project_id
            id_preserved = True
        else:
            # KNOWN GAP: `services.projects.ProjectStore.create()` always
            # mints its own id (no `project_id=` parameter exists there to
            # accept this bundle's original one) — see this lot's report for
            # the exact change that would close this. A brand-new machine
            # therefore gets a NEW project_id; `original_project_id` is
            # still reported so a caller can reconcile it by hand.
            project = st.create(
                name=project_name or manifest.get("project_name") or original_project_id or "Imported project",
                workspace=target_workspace, owner=owner,
            )
            project_id = str(project["id"])
            id_preserved = False

        written_files = 0
        for entry in manifest.get("files") or []:
            zip_name = f"files/{entry['path']}"
            if zip_name not in zf.namelist():
                continue  # already named in lost_references
            dest = os.path.join(target_workspace, *entry["path"].split("/"))
            if not os.path.commonpath([os.path.realpath(target_workspace), os.path.realpath(dest)]) \
                    == os.path.realpath(target_workspace):
                continue  # never write outside the target workspace
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(zf.read(zip_name))
            written_files += 1

        imported_chat_ids: List[str] = []
        if "chats.json" in zf.namelist():
            imported_chat_ids = _import_chats(json.loads(zf.read("chats.json")), project_id, owner)

        project_identity.write_marker(target_workspace, project_id, force=True)

        report.update({
            "dry_run": False,
            "project_id": project_id,
            "original_project_id": original_project_id,
            "id_preserved": id_preserved,
            "workspace": target_workspace,
            "files_written": written_files,
            "chats_imported": len(imported_chat_ids),
            "imported_chat_ids": imported_chat_ids,
        })
        return report


def _import_chats(chats: List[Dict[str, Any]], project_id: str, owner: Optional[str]) -> List[str]:
    """Fresh session/message ids on import — an id collision with an
    existing session on this machine must never silently overwrite it."""
    from core.database import SessionLocal, Session as DbSession, ChatMessage as DbChatMessage
    db = SessionLocal()
    new_ids: List[str] = []
    try:
        for chat in chats:
            new_session_id = uuid.uuid4().hex
            db.add(DbSession(
                id=new_session_id, name=chat.get("name") or "Imported chat",
                endpoint_url="", model=chat.get("model") or "", owner=owner,
                folder=chat.get("folder"), project_id=project_id, archived=bool(chat.get("archived")),
            ))
            for m in chat.get("messages") or []:
                db.add(DbChatMessage(
                    id=uuid.uuid4().hex, session_id=new_session_id,
                    role=m.get("role") or "user", content=m.get("content") or "",
                    meta_data=m.get("metadata"),
                ))
            new_ids.append(new_session_id)
        db.commit()
        return new_ids
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

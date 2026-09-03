"""Projects — bind a chat folder, a workspace directory, standing instructions
and a file-backed memory into a single object.

Why it is built this way
------------------------
* **Storage is a plain JSON file** (``data/projects.json``), not a new DB table.
  Every other user-config surface here already does that (presets.json,
  settings.json, memory.json), it needs no migration, and it keeps the feature
  additive so it rebases cheaply against a fast-moving upstream.

* **Chats are bound through the existing ``sessions.folder`` column.** No schema
  change: the sidebar folder *is* the project's chat group. One project owns one
  folder name.

* **Project memory is Markdown on disk** under ``<workspace>/.odysseus/`` rather
  than rows in the ``memories`` table. Files are greppable, hand-editable,
  survive a database wipe, travel with the folder when it moves — and the agent
  already has read/write tools confined to the project's work roots, so it can
  maintain them like any other project file.

Nothing here imports from ``src.*`` beyond ``tool_execution.vet_workspace`` (done
lazily, inside the function) so the module stays importable from route setup
without dragging the app's init order around.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Directory created inside a project's workspace to hold its memory.
MEMORY_DIRNAME = ".odysseus"
MEMORY_INDEX = "MEMORY.md"

# Caps. The injected block rides in the system prompt of every turn, and Luis's
# local models run 32k contexts — an unbounded index would quietly eat the
# window. Truncation is announced in-band so the model knows it saw a partial
# file rather than a complete one.
MAX_INSTRUCTIONS = 10000
MAX_INDEX_INJECT = 4000
MAX_MEMORY_FILE = 200000
MAX_CONTEXT_ITEMS = 40
MAX_CONTEXT_READ_LINES = 500
MAX_CONTEXT_SEARCH_FILES = 500
MAX_CONTEXT_SEARCH_MATCHES = 80

_NAME_RE = re.compile(r"^[^\x00-\x1f<>:\"/\\|?*]{1,80}$")
_MEM_FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}\.md$")

# Per-project agent knobs (routes/chat_routes builds `harness_options` from
# them). Missing keys mean "use the global setting / default".
#   trusted        — file writes inside the workspace skip the approval gate
#   trusted_agents — same for delegate_agents (its workers keep their gates)
#   review_mode    — edits stay "pending" until accepted per file in the viewer
#   checkpoints    — shadow snapshot before the first change of each turn
#   run_tests      — run the project's tests after a turn with changes
#   test_command   — explicit test command (empty = auto-detect)
#   review_model   — auto-review reviewer ("", "same" or a model name)
AGENT_OPTION_FIELDS: Dict[str, type] = {
    "trusted": bool, "trusted_agents": bool, "review_mode": bool, "checkpoints": bool,
    "run_tests": bool, "test_command": str, "review_model": str,
}
AGENT_OPTION_DEFAULTS: Dict[str, Any] = {
    "trusted": False, "trusted_agents": False, "review_mode": False, "checkpoints": True,
    "run_tests": True, "test_command": "", "review_model": "",
}


def agent_options(project: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The project's agent knobs with defaults filled in ({} for no project)."""
    if not project:
        return {}
    out: Dict[str, Any] = {}
    for key, kind in AGENT_OPTION_FIELDS.items():
        val = project.get(key, AGENT_OPTION_DEFAULTS[key])
        out[key] = bool(val) if kind is bool else str(val or "").strip()
    out["project_id"] = project.get("id")
    return out


class ProjectError(ValueError):
    """Invalid project input — routes map this to a 400."""


def _now() -> int:
    return int(time.time())


class ProjectStore:
    """Load/save projects and read/write their on-disk memory."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "projects.json")
        self._cache: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> List[Dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        rows: List[Dict[str, Any]] = []
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    rows = [r for r in data if isinstance(r, dict)]
                elif isinstance(data, dict) and isinstance(data.get("projects"), list):
                    rows = [r for r in data["projects"] if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError) as e:
            # A corrupt file must not take the app down. Start empty and keep
            # the broken copy so nothing is silently destroyed on next save.
            logger.error("projects.json unreadable (%s); starting empty", e)
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            rows = []
        # New presentation-only fields stay backwards compatible with the
        # first projects.json format.  Normalising them here means every API
        # consumer sees a stable shape without forcing a migration or an
        # eager rewrite of the user's file.
        for row in rows:
            row.setdefault("pinned", False)
            row.setdefault("archived", False)
            row.setdefault("context_items", [])
        self._cache = rows
        return rows

    def _save(self, rows: List[Dict[str, Any]]) -> None:
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)   # atomic: no half-written projects.json
        self._cache = rows

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @staticmethod
    def _owned(row: Dict[str, Any], owner: Optional[str]) -> bool:
        # owner=None on a row is legacy/single-user and visible to everyone,
        # matching how sessions/documents treat their nullable owner column.
        row_owner = row.get("owner")
        return row_owner is None or owner is None or row_owner == owner

    def list(self, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = [r for r in self._load() if self._owned(r, owner)]
        return sorted(rows, key=lambda r: (r.get("name") or "").lower())

    def get(self, project_id: str, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
        for r in self._load():
            if r.get("id") == project_id and self._owned(r, owner):
                return r
        return None

    def get_by_folder(self, folder: str, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Resolve the project that owns a sidebar folder. Case-insensitive:
        folder names are typed by hand in the UI and 'LocalAI' / 'localai'
        must not become two different projects."""
        key = (folder or "").strip().casefold()
        if not key:
            return None
        for r in self._load():
            if (r.get("folder") or "").strip().casefold() == key and self._owned(r, owner):
                return r
        return None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _validate(
        self,
        name: str,
        folder: str,
        workspace: str,
        instructions: str,
        *,
        owner: Optional[str],
        exclude_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        name = (name or "").strip()
        folder = (folder or "").strip() or name
        workspace = (workspace or "").strip()
        instructions = (instructions or "").strip()

        if not _NAME_RE.match(name):
            raise ProjectError("Project name must be 1-80 chars and contain no path separators")
        if not _NAME_RE.match(folder):
            raise ProjectError("Folder name must be 1-80 chars and contain no path separators")
        if len(instructions) > MAX_INSTRUCTIONS:
            raise ProjectError(f"Instructions exceed {MAX_INSTRUCTIONS} characters")

        # The workspace is vetted with the SAME function the chat path uses, so
        # a project can never bind a path that a manual /workspace set would
        # have refused (filesystem root, .ssh, non-directory, ...).
        resolved = ""
        if workspace:
            from src.tool_execution import vet_workspace
            resolved = vet_workspace(workspace) or ""
            if not resolved:
                raise ProjectError(
                    f"'{workspace}' is not a usable project folder "
                    "(must be an existing directory, not a drive root or a sensitive path)"
                )

        key = folder.casefold()
        for r in self._load():
            if r.get("id") == exclude_id or not self._owned(r, owner):
                continue
            if (r.get("folder") or "").strip().casefold() == key:
                raise ProjectError(
                    f"Folder '{folder}' already belongs to project '{r.get('name')}'"
                )

        return {
            "name": name,
            "folder": folder,
            "workspace": resolved,
            "instructions": instructions,
        }

    def create(
        self,
        name: str,
        folder: str = "",
        workspace: str = "",
        instructions: str = "",
        owner: Optional[str] = None,
        scaffold_memory: bool = True,
    ) -> Dict[str, Any]:
        fields = self._validate(name, folder, workspace, instructions, owner=owner)
        row = {
            "id": uuid.uuid4().hex[:12],
            "owner": owner,
            "enabled": True,
            "pinned": False,
            "archived": False,
            "context_items": [],
            "created_at": _now(),
            "updated_at": _now(),
            **fields,
        }
        rows = list(self._load())
        rows.append(row)
        self._save(rows)
        if scaffold_memory and row["workspace"]:
            try:
                self.scaffold_memory(row)
            except OSError as e:
                # A read-only or full disk shouldn't fail project creation —
                # the project still works, it just has no memory folder yet.
                logger.warning("Could not scaffold memory for %s: %s", row["name"], e)
        return row

    def update(
        self,
        project_id: str,
        updates: Dict[str, Any],
        owner: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        rows = list(self._load())
        for i, r in enumerate(rows):
            if r.get("id") != project_id or not self._owned(r, owner):
                continue
            merged = {
                "name": updates.get("name", r.get("name", "")),
                "folder": updates.get("folder", r.get("folder", "")),
                "workspace": updates.get("workspace", r.get("workspace", "")),
                "instructions": updates.get("instructions", r.get("instructions", "")),
            }
            fields = self._validate(
                merged["name"], merged["folder"], merged["workspace"],
                merged["instructions"], owner=r.get("owner"), exclude_id=project_id,
            )
            new_row = dict(r)
            new_row.update(fields)
            # Agent knobs (all optional; see AGENT_OPTION_FIELDS).
            for key, kind in AGENT_OPTION_FIELDS.items():
                if key not in updates:
                    continue
                val = updates[key]
                if kind is bool:
                    new_row[key] = bool(val)
                else:
                    text = str(val or "").strip()
                    if len(text) > 400:
                        raise ProjectError(f"{key} is too long (max 400 chars)")
                    new_row[key] = text
            if "enabled" in updates:
                new_row["enabled"] = bool(updates["enabled"])
            if "archived" in updates:
                new_row["archived"] = bool(updates["archived"])
                # Archived projects do not occupy the pinned section.  Their
                # chats keep resolving to the project; archive is an
                # organisation state, not a context kill-switch.
                if new_row["archived"]:
                    new_row["pinned"] = False
            if "pinned" in updates:
                new_row["pinned"] = bool(updates["pinned"])
                if new_row["pinned"]:
                    new_row["archived"] = False
            new_row["updated_at"] = _now()
            rows[i] = new_row
            self._save(rows)
            return new_row
        return None

    # ------------------------------------------------------------------
    # Project work roots (files and folders)
    # ------------------------------------------------------------------

    @staticmethod
    def _context_item(project: Dict[str, Any], item_id: str) -> Dict[str, Any]:
        for item in (project or {}).get("context_items") or []:
            if isinstance(item, dict) and item.get("id") == item_id:
                return item
        raise ProjectError("Context item not found")

    @staticmethod
    def _context_target(item: Dict[str, Any], relative_path: str = "") -> str:
        from src.tool_execution import vet_project_root

        base = vet_project_root(item.get("path") or "")
        if not base:
            raise ProjectError("This context item no longer exists or is no longer safe")
        relative_path = (relative_path or "").strip()
        if os.path.isfile(base):
            if relative_path not in ("", ".", os.path.basename(base)):
                raise ProjectError("A file context item has no child paths")
            return base

        target = os.path.realpath(os.path.join(base, relative_path)) if relative_path else base
        try:
            if os.path.commonpath([target, base]) != base:
                raise ValueError
        except ValueError:
            raise ProjectError("Context path escapes the attached folder")
        vetted = vet_project_root(target)
        if not vetted:
            raise ProjectError("Context path does not exist or is sensitive")
        return vetted

    def add_context_item(
        self, project_id: str, path: str, owner: Optional[str] = None
    ) -> Dict[str, Any]:
        from src.tool_execution import vet_project_root

        resolved = vet_project_root(path)
        if not resolved:
            raise ProjectError(
                "Context must be an existing file or folder, not a filesystem root or sensitive path"
            )
        rows = list(self._load())
        for i, row in enumerate(rows):
            if row.get("id") != project_id or not self._owned(row, owner):
                continue
            items = [item for item in (row.get("context_items") or []) if isinstance(item, dict)]
            if os.path.normcase(row.get("workspace") or "") == os.path.normcase(resolved):
                raise ProjectError("This folder is already the project's primary working folder")
            for item in items:
                if os.path.normcase(item.get("path") or "") == os.path.normcase(resolved):
                    return item
            if len(items) >= MAX_CONTEXT_ITEMS:
                raise ProjectError(f"A project can have at most {MAX_CONTEXT_ITEMS} context items")
            item = {
                "id": uuid.uuid4().hex[:10],
                "path": resolved,
                "kind": "folder" if os.path.isdir(resolved) else "file",
                "name": os.path.basename(resolved) or resolved,
            }
            updated = dict(row)
            updated["context_items"] = [*items, item]
            updated["updated_at"] = _now()
            rows[i] = updated
            self._save(rows)
            return item
        raise ProjectError("Project not found")

    def remove_context_item(
        self, project_id: str, item_id: str, owner: Optional[str] = None
    ) -> bool:
        rows = list(self._load())
        for i, row in enumerate(rows):
            if row.get("id") != project_id or not self._owned(row, owner):
                continue
            items = [item for item in (row.get("context_items") or []) if isinstance(item, dict)]
            kept = [item for item in items if item.get("id") != item_id]
            if len(kept) == len(items):
                return False
            updated = dict(row)
            updated["context_items"] = kept
            updated["updated_at"] = _now()
            rows[i] = updated
            self._save(rows)
            return True
        return False

    def list_context_path(
        self, project: Dict[str, Any], item_id: str = "", relative_path: str = ""
    ) -> Dict[str, Any]:
        items = [item for item in (project or {}).get("context_items") or [] if isinstance(item, dict)]
        if not item_id:
            return {"items": items}
        item = self._context_item(project, item_id)
        target = self._context_target(item, relative_path)
        if os.path.isfile(target):
            st = os.stat(target)
            return {"item_id": item_id, "path": relative_path or os.path.basename(target), "kind": "file", "size": st.st_size}
        entries = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    try:
                        entries.append({
                            "name": entry.name,
                            "kind": "folder" if entry.is_dir(follow_symlinks=False) else "file",
                            "size": entry.stat(follow_symlinks=False).st_size if entry.is_file(follow_symlinks=False) else None,
                        })
                    except OSError:
                        continue
                    if len(entries) >= 200:
                        break
        except OSError as exc:
            raise ProjectError(f"Could not list context folder: {exc}")
        entries.sort(key=lambda entry: (entry["kind"] != "folder", entry["name"].casefold()))
        return {"item_id": item_id, "path": relative_path, "kind": "folder", "entries": entries}

    def read_context_file(
        self,
        project: Dict[str, Any],
        item_id: str,
        relative_path: str = "",
        start_line: int = 1,
        line_count: int = 200,
    ) -> Dict[str, Any]:
        item = self._context_item(project, item_id)
        target = self._context_target(item, relative_path)
        if not os.path.isfile(target):
            raise ProjectError("Choose a file inside the attached context folder")
        try:
            with open(target, "rb") as fh:
                sample = fh.read(4096)
            if b"\x00" in sample:
                raise ProjectError("This appears to be a binary file and cannot be read as text")
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except ProjectError:
            raise
        except OSError as exc:
            raise ProjectError(f"Could not read context file: {exc}")
        start = max(1, int(start_line or 1))
        count = max(1, min(int(line_count or 200), MAX_CONTEXT_READ_LINES))
        selected = lines[start - 1:start - 1 + count]
        numbered = "".join(f"{number}: {line}" for number, line in enumerate(selected, start=start))
        return {
            "item_id": item_id,
            "path": relative_path or os.path.basename(target),
            "start_line": start,
            "line_count": len(selected),
            "total_lines": len(lines),
            "content": numbered,
        }

    def search_context(
        self, project: Dict[str, Any], query: str, item_id: str = ""
    ) -> Dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise ProjectError("Search query is required")
        items = [self._context_item(project, item_id)] if item_id else [
            item for item in (project or {}).get("context_items") or [] if isinstance(item, dict)
        ]
        matches = []
        scanned = 0
        needle = query.casefold()
        for item in items:
            base = self._context_target(item)
            if os.path.isfile(base):
                candidates = [(base, os.path.basename(base))]
            else:
                candidates = []
                for root, dirs, files in os.walk(base, followlinks=False):
                    dirs[:] = [name for name in dirs if not name.startswith(".") and not os.path.islink(os.path.join(root, name))]
                    for name in files:
                        full = os.path.join(root, name)
                        if name.startswith(".") or os.path.islink(full):
                            continue
                        candidates.append((full, os.path.relpath(full, base)))
                        if len(candidates) + scanned >= MAX_CONTEXT_SEARCH_FILES:
                            break
                    if len(candidates) + scanned >= MAX_CONTEXT_SEARCH_FILES:
                        break
            for full, relative in candidates:
                if scanned >= MAX_CONTEXT_SEARCH_FILES or len(matches) >= MAX_CONTEXT_SEARCH_MATCHES:
                    break
                scanned += 1
                try:
                    if os.path.getsize(full) > 2_000_000:
                        continue
                    with open(full, "rb") as fh:
                        if b"\x00" in fh.read(4096):
                            continue
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        for number, line in enumerate(fh, start=1):
                            if needle in line.casefold():
                                matches.append({
                                    "item_id": item.get("id"),
                                    "path": relative,
                                    "line": number,
                                    "snippet": line.strip()[:300],
                                })
                                if len(matches) >= MAX_CONTEXT_SEARCH_MATCHES:
                                    break
                except OSError:
                    continue
        return {"query": query, "matches": matches, "scanned_files": scanned, "truncated": scanned >= MAX_CONTEXT_SEARCH_FILES or len(matches) >= MAX_CONTEXT_SEARCH_MATCHES}

    def touch(self, project_id: str, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Refresh activity ordering without changing project settings."""
        rows = list(self._load())
        for i, row in enumerate(rows):
            if row.get("id") != project_id or not self._owned(row, owner):
                continue
            touched = dict(row)
            touched["updated_at"] = _now()
            rows[i] = touched
            self._save(rows)
            return touched
        return None

    def delete(self, project_id: str, owner: Optional[str] = None) -> bool:
        """Forget the project. Never touches the workspace folder or its
        memory files on disk — deleting a row must not delete the user's work."""
        rows = self._load()
        kept = [r for r in rows if not (r.get("id") == project_id and self._owned(r, owner))]
        if len(kept) == len(rows):
            return False
        self._save(kept)
        return True

    # ------------------------------------------------------------------
    # Memory on disk
    # ------------------------------------------------------------------

    def memory_dir(self, project: Dict[str, Any]) -> str:
        ws = (project or {}).get("workspace") or ""
        return os.path.join(ws, MEMORY_DIRNAME) if ws else ""

    def _memory_path(self, project: Dict[str, Any], filename: str) -> str:
        """Resolve a memory filename to an absolute path, refusing anything
        that would escape the memory directory. The filename pattern already
        excludes separators and '..', but the realpath check is kept as the
        actual boundary — pattern matching is a filter, not a confinement."""
        if not _MEM_FILE_RE.match(filename or ""):
            raise ProjectError("Memory filenames must look like 'topic.md'")
        base = self.memory_dir(project)
        if not base:
            raise ProjectError("Project has no folder bound, so it has no memory")
        full = os.path.realpath(os.path.join(base, filename))
        if os.path.commonpath([full, os.path.realpath(base)]) != os.path.realpath(base):
            raise ProjectError("Memory path escapes the project memory directory")
        return full

    def scaffold_memory(self, project: Dict[str, Any]) -> str:
        """Create <workspace>/.odysseus/MEMORY.md if it isn't there yet."""
        base = self.memory_dir(project)
        if not base:
            return ""
        os.makedirs(base, exist_ok=True)
        index = os.path.join(base, MEMORY_INDEX)
        if not os.path.exists(index):
            with open(index, "w", encoding="utf-8") as fh:
                fh.write(
                    f"# {project.get('name', 'Project')} — memory index\n\n"
                    "One line per topic file, newest concerns first. Keep this file\n"
                    "short: it is injected into every chat in this project. Detail\n"
                    "belongs in the topic files it points at.\n\n"
                    "<!-- - [Title](topic.md) — one-line hook -->\n"
                )
        return index

    def read_index(self, project: Dict[str, Any]) -> str:
        base = self.memory_dir(project)
        if not base:
            return ""
        path = os.path.join(base, MEMORY_INDEX)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read(MAX_MEMORY_FILE)
        except OSError:
            return ""

    def list_memory_files(self, project: Dict[str, Any]) -> List[Dict[str, Any]]:
        base = self.memory_dir(project)
        if not base or not os.path.isdir(base):
            return []
        out = []
        try:
            with os.scandir(base) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".md"):
                        try:
                            st = entry.stat()
                            out.append({
                                "name": entry.name,
                                "size": st.st_size,
                                "modified": int(st.st_mtime),
                            })
                        except OSError:
                            continue
        except OSError:
            return []
        # Index first, then the rest alphabetically.
        out.sort(key=lambda f: (f["name"] != MEMORY_INDEX, f["name"].lower()))
        return out

    def read_memory_file(self, project: Dict[str, Any], filename: str) -> str:
        path = self._memory_path(project, filename)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read(MAX_MEMORY_FILE)
        except FileNotFoundError:
            return ""
        except OSError as e:
            raise ProjectError(f"Could not read {filename}: {e}")

    def write_memory_file(self, project: Dict[str, Any], filename: str, content: str) -> None:
        path = self._memory_path(project, filename)
        if len(content or "") > MAX_MEMORY_FILE:
            raise ProjectError(f"Memory file exceeds {MAX_MEMORY_FILE} bytes")
        is_new_topic = filename != MEMORY_INDEX and not os.path.exists(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(content or "")
            os.replace(tmp, path)
        except OSError as e:
            raise ProjectError(f"Could not write {filename}: {e}")

        # A note that is absent from MEMORY.md is effectively invisible to the
        # model: only the short index is injected on every turn.  Notes created
        # through the Projects UI therefore get a conservative one-line index
        # entry automatically. Existing files and hand-written index wording
        # are never changed.
        if is_new_topic:
            try:
                self.scaffold_memory(project)
                index_path = self._memory_path(project, MEMORY_INDEX)
                with open(index_path, "r", encoding="utf-8", errors="replace") as fh:
                    index = fh.read(MAX_MEMORY_FILE)
                marker = f"]({filename})"
                if marker not in index:
                    title = os.path.splitext(filename)[0].replace("-", " ").replace("_", " ").strip().title()
                    updated = index.rstrip() + f"\n\n- [{title}]({filename}) — project note\n"
                    if len(updated) <= MAX_MEMORY_FILE:
                        index_tmp = index_path + ".tmp"
                        with open(index_tmp, "w", encoding="utf-8") as fh:
                            fh.write(updated)
                        os.replace(index_tmp, index_path)
                    else:
                        logger.warning("Could not index %s: %s is at its size limit", filename, MEMORY_INDEX)
            except OSError as e:
                # The note itself is already durable and remains visible in
                # the UI. An index maintenance failure should not discard it.
                logger.warning("Could not index project memory file %s: %s", filename, e)

    # ------------------------------------------------------------------
    # The block injected into the system prompt
    # ------------------------------------------------------------------

    def system_block(self, project: Dict[str, Any]) -> str:
        """Render the project's standing context.

        KV-cache note: everything in here is static for the life of a project —
        no timestamps, no per-turn counts, no retrieved snippets — so the system
        prefix stays byte-identical across the turns of a chat and local
        backends keep reusing their cached prefix. It only changes when the user
        edits the project or its MEMORY.md, which is exactly when it should.
        """
        if not project or not project.get("enabled", True):
            return ""

        name = project.get("name") or "Untitled"
        workspace = project.get("workspace") or ""
        folder = project.get("folder") or name
        instructions = (project.get("instructions") or "").strip()
        context_items = [item for item in (project.get("context_items") or []) if isinstance(item, dict)]

        parts = [f'You are working inside the project "{name}".']
        if workspace:
            parts.append(
                f"Project folder: {workspace}\n"
                "Your file tools are confined to it. Paths the user gives without a "
                "drive or leading slash are relative to this folder."
            )
        parts.append(f'Chats for this project are grouped under the sidebar folder "{folder}".')

        parts.append(
            "## Previous project chats\n"
            "You can consult earlier conversations from this project on demand with "
            "`search_project_chats`. Use it when a question refers to prior decisions, "
            "earlier attempts or what was discussed before; do not guess from titles."
        )

        if context_items:
            manifest = "\n".join(
                f'- `{item.get("id")}` ({item.get("kind", "file")}): {item.get("path", "")}'
                for item in context_items
            )
            parts.append(
                "## Project work roots\n"
                "These attached files and folders are part of the working project. "
                "You may inspect them with `project_context`, and you may read or "
                "modify them with the normal file tools. Relative paths resolve in "
                "the primary project folder; use the absolute paths below for other "
                "roots. Their contents are not copied into the prompt.\n" + manifest
            )

        if instructions:
            parts.append("## Project instructions\n" + instructions)

        if workspace:
            # Project objectives dashboard (services/objectives.py). Only
            # present when the project has at least one non-dropped objective;
            # the section only changes when the objectives change, so the
            # KV-cache note below stays true enough. A broken objectives file
            # must cost the section, never the message.
            try:
                from services import objectives as _objectives
                obj_block = _objectives.objectives_block(project)
                if obj_block:
                    parts.append(obj_block)
            except Exception as e:  # noqa: BLE001 - prompt path, never raise
                logger.debug("objectives_block failed for %s: %s", name, e)

        if workspace:
            parts.append(
                "## Project memory\n"
                f"Durable notes for this project live as Markdown in "
                f"{os.path.join(workspace, MEMORY_DIRNAME)}. {MEMORY_INDEX} is the "
                "index; each topic has its own file beside it.\n"
                "Read a topic file with your file tools before answering when its "
                "index line looks relevant - the index alone is not the content.\n"
                "Write what is worth having in the NEXT chat: decisions and the "
                "reasoning behind them, constraints you discovered, approaches that "
                "turned out not to work, where external things live. Add a one-line "
                f"index entry in {MEMORY_INDEX} for every new file. Do not record "
                "in-progress task state, or anything that could be re-derived by "
                "reading the project's own files."
            )
            index = self.read_index(project).strip()
            if index:
                if len(index) > MAX_INDEX_INJECT:
                    index = (
                        index[:MAX_INDEX_INJECT]
                        + f"\n\n[...{MEMORY_INDEX} truncated here — read the file for the rest]"
                    )
                parts.append(f"### {MEMORY_INDEX}\n{index}")

        return "<project_context>\n" + "\n\n".join(parts) + "\n</project_context>"


# ----------------------------------------------------------------------
# Module-level singleton + the two helpers the chat path calls
# ----------------------------------------------------------------------

_store: Optional[ProjectStore] = None


def get_store() -> ProjectStore:
    global _store
    if _store is None:
        from core.constants import DATA_DIR
        _store = ProjectStore(DATA_DIR)
    return _store


def project_for_session(session_id: str, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve a chat session to its project via the session's folder.

    Deliberately swallows every failure: this runs on the hot chat path, and a
    broken projects.json or a missing session must degrade to "no project",
    never to a failed chat.
    """
    if not session_id:
        return None
    try:
        from core.database import Session as SessionModel, SessionLocal
        db = SessionLocal()
        try:
            row = db.query(SessionModel).filter(SessionModel.id == session_id).first()
            folder = getattr(row, "folder", None) if row else None
        finally:
            db.close()
        if not folder:
            return None
        project = get_store().get_by_folder(folder, owner)
        if project and project.get("enabled", True):
            return project
        return None
    except Exception as e:  # noqa: BLE001 - hot path, never raise
        logger.debug("project_for_session(%s) failed: %s", session_id, e)
        return None


def workspace_for_session(session_id: str, owner: Optional[str] = None) -> str:
    """The project's workspace for this chat, or '' when there is none."""
    project = project_for_session(session_id, owner)
    return (project or {}).get("workspace") or ""


def work_roots_for_session(session_id: str, owner: Optional[str] = None) -> List[str]:
    """Canonical file/folder roots available to this project's tools."""
    project = project_for_session(session_id, owner)
    if not project:
        return []
    from src.tool_execution import vet_project_root

    roots: List[str] = []
    for candidate in [
        project.get("workspace") or "",
        *[item.get("path") or "" for item in (project.get("context_items") or []) if isinstance(item, dict)],
    ]:
        vetted = vet_project_root(candidate)
        if vetted and vetted not in roots:
            roots.append(vetted)
    return roots


def instructions_for_session(session_id: str, owner: Optional[str] = None) -> str:
    """The project's system block for this chat, or '' when there is none."""
    project = project_for_session(session_id, owner)
    if not project:
        return ""
    try:
        return get_store().system_block(project)
    except Exception as e:  # noqa: BLE001 - hot path, never raise
        logger.debug("system_block failed for %s: %s", project.get("name"), e)
        return ""

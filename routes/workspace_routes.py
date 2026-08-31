"""Workspace API - browse server directories to pick a tool workspace folder."""
import os
from fastapi import APIRouter, Request, HTTPException, Query

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user

# Cap entries returned per directory (mirrors filesystem_tools._CODENAV_MAX_HITS).
# A huge directory shouldn't dump thousands of rows into the picker; the user can
# type/paste a path to jump straight in instead.
_MAX_BROWSE_DIRS = 500


def setup_workspace_routes():
    router = APIRouter(prefix="/api/workspace", tags=["workspace"])

    @router.get("/browse")
    def browse(
        request: Request,
        path: str = Query(default=""),
        include_files: bool = Query(default=False),
    ):
        """List subdirectories of `path` (default: home) so the UI can navigate
        the server filesystem and pick a workspace folder. Directories only.

        ADMIN-ONLY: this enumerates the server filesystem, so it is gated the
        same way the file/shell tools are (read_file/write_file/bash are in
        NON_ADMIN_BLOCKED_TOOLS). A non-admin who can't use those tools must not
        be able to map the host's directory tree either.
        """
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace browsing is admin-only")

        # Resolve symlinks so the reported path is canonical and the UI navigates
        # real directories (defends against symlink games in displayed paths).
        target = os.path.realpath(os.path.expanduser(path.strip() or "~"))
        if not os.path.isdir(target):
            target = os.path.realpath(os.path.expanduser("~"))

        dirs = []
        files = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        # Don't follow symlinks when classifying - a symlinked
                        # dir is skipped rather than letting the browser wander
                        # off via a link. Hidden entries are omitted.
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                            # Build the child path server-side with os.path.join
                            # so it's correct on Windows (backslashes) and Linux.
                            dirs.append({"name": entry.name, "path": os.path.join(target, entry.name)})
                        elif include_files and entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                            files.append({
                                "name": entry.name,
                                "path": os.path.join(target, entry.name),
                                "size": entry.stat(follow_symlinks=False).st_size,
                            })
                    except OSError:
                        continue
        except (PermissionError, OSError):
            dirs = []

        dirs_sorted = sorted(dirs, key=lambda d: d["name"].lower())
        truncated = len(dirs_sorted) > _MAX_BROWSE_DIRS
        parent = os.path.dirname(target)
        from src.tool_execution import vet_workspace
        return {
            "path": target,
            "parent": parent if parent and parent != target else None,
            "dirs": dirs_sorted[:_MAX_BROWSE_DIRS],
            "files": sorted(files, key=lambda f: f["name"].lower())[:_MAX_BROWSE_DIRS],
            "truncated": truncated,
            # Whether this directory may be bound as a workspace (filesystem
            # roots and sensitive dirs may be browsed through but not chosen).
            "selectable": vet_workspace(target) is not None,
        }

    @router.get("/vet")
    def vet(request: Request, path: str = Query(default="")):
        """Validate a workspace path without binding it.

        The UI calls this before persisting a manually typed path (/workspace
        set) so a typo, file path, deleted folder, sensitive dir, or filesystem
        root is rejected up front with the canonical path returned on success,
        instead of being stored client-side and silently dropped at chat time.
        Admin-gated like /browse: it confirms path existence on the host.
        """
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace selection is admin-only")
        from src.tool_execution import vet_workspace
        resolved = vet_workspace(path)
        return {"ok": resolved is not None, "path": resolved}

    @router.get("/vet-context")
    def vet_context(request: Request, path: str = Query(default="")):
        """Validate a project work-root file or directory."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Context selection is admin-only")
        from src.tool_execution import vet_readonly_context
        resolved = vet_readonly_context(path)
        return {
            "ok": resolved is not None,
            "path": resolved,
            "kind": (
                "folder" if resolved and os.path.isdir(resolved)
                else "file" if resolved else None
            ),
        }

    # ── File viewer for the chat's "Edited N files" cards ─────────────────
    _FILE_VIEW_MAX = 400_000

    def _confine(workspace: str, path: str) -> str:
        from src.tool_execution import vet_workspace, _resolve_tool_path_in_roots
        root = vet_workspace(workspace or "")
        if not root:
            raise HTTPException(status_code=400, detail="workspace is not a valid folder")
        try:
            return _resolve_tool_path_in_roots([root], path, root)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/file")
    def read_workspace_file(
        request: Request,
        workspace: str = Query(default=""),
        path: str = Query(default=""),
    ):
        """Text of one file inside the bound workspace (review panel). Same
        admin gate and path confinement as the agent's read_file."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace files are admin-only")
        target = _confine(workspace, path)
        if not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="file not found")
        size = os.path.getsize(target)
        try:
            with open(target, "rb") as f:
                raw = f.read(_FILE_VIEW_MAX + 1)
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
        truncated = len(raw) > _FILE_VIEW_MAX
        raw = raw[:_FILE_VIEW_MAX]
        binary = b"\x00" in raw[:8000]
        text = "" if binary else raw.decode("utf-8", errors="replace")
        root = os.path.realpath(os.path.expanduser(workspace))
        rel = os.path.relpath(target, root).replace(os.sep, "/")
        return {
            "path": target, "rel": rel, "workspace": root, "size": size,
            "binary": binary, "truncated": truncated, "text": text,
            "lines": (text.count("\n") + (1 if text and not text.endswith("\n") else 0)) if text else 0,
            "mtime": os.path.getmtime(target),
        }

    @router.get("/file_diff")
    def workspace_file_diff(
        request: Request,
        workspace: str = Query(default=""),
        path: str = Query(default=""),
    ):
        """`git diff` of one file against HEAD (working tree), or the whole file
        as added when it is untracked / the folder is not a git repo."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace files are admin-only")
        target = _confine(workspace, path)
        root = os.path.realpath(os.path.expanduser(workspace))
        import subprocess
        try:
            probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        except (OSError, subprocess.SubprocessError):
            probe = None
        if not probe or probe.returncode != 0:
            return {"git": False, "diff": "", "status": None}
        top = os.path.realpath(probe.stdout.strip())
        rel = os.path.relpath(target, top).replace(os.sep, "/")
        try:
            st = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=top,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
            code = (st.stdout or "")[:2].strip() or None
            if code and code.startswith("?"):
                diff = subprocess.run(["git", "diff", "--no-index", "--", "/dev/null" if os.name != "nt" else "NUL", rel],
                                      cwd=top, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
            else:
                diff = subprocess.run(["git", "diff", "--", rel], cwd=top, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=8)
            text = diff.stdout or ""
        except (OSError, subprocess.SubprocessError) as e:
            raise HTTPException(status_code=500, detail=str(e))
        if len(text) > _FILE_VIEW_MAX:
            text = text[:_FILE_VIEW_MAX] + "\n… diff truncated"
        return {"git": True, "diff": text, "status": code, "rel": rel}

    @router.post("/reveal")
    def reveal_in_folder(
        request: Request,
        workspace: str = Query(default=""),
        path: str = Query(default=""),
    ):
        """Open the OS file manager with the file selected (Odysseus runs on
        the same machine as the browser in the local setup)."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Admin-only")
        target = _confine(workspace, path)
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail="not found")
        import subprocess, sys as _sys
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", target])
            elif _sys.platform == "darwin":
                subprocess.Popen(["open", "-R", target])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(target)])
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {"ok": True}

    return router

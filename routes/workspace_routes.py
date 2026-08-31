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
    import re as _re
    _SHA_RE = _re.compile(r"^[0-9a-fA-F]{7,40}$")

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
        # Display only: normalise CRLF so line numbers and the diff view agree.
        text = "" if binary else raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        crlf = (not binary) and (b"\r\n" in raw)
        root = os.path.realpath(os.path.expanduser(workspace))
        rel = os.path.relpath(target, root).replace(os.sep, "/")
        return {
            "path": target, "rel": rel, "workspace": root, "size": size,
            "binary": binary, "truncated": truncated, "text": text, "crlf": crlf,
            "lines": (text.count("\n") + (1 if text and not text.endswith("\n") else 0)) if text else 0,
            "mtime": os.path.getmtime(target),
        }

    @router.get("/file_diff")
    def workspace_file_diff(
        request: Request,
        workspace: str = Query(default=""),
        path: str = Query(default=""),
        checkpoint: str = Query(default=""),
    ):
        """`git diff` of one file against HEAD (working tree), or the whole file
        as added when it is untracked / the folder is not a git repo. With a
        `checkpoint` sha (the turn's shadow snapshot) the diff is taken against
        that baseline instead — exactly "what this turn changed", and it works
        in folders that are not repositories."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace files are admin-only")
        target = _confine(workspace, path)
        root = os.path.realpath(os.path.expanduser(workspace))
        if checkpoint:
            from src import workspace_checkpoints as wc
            if not _SHA_RE.match(checkpoint):
                raise HTTPException(status_code=400, detail="bad checkpoint")
            rel = os.path.relpath(target, root).replace(os.sep, "/")
            text = wc.diff_since(root, checkpoint, rel, max_chars=_FILE_VIEW_MAX)
            changed = wc.changed_since(root, checkpoint, [rel])
            code = changed[0]["status"] if changed else None
            if not text and not os.path.exists(target) and not wc.exists_at(root, checkpoint, rel):
                return {"git": False, "diff": "", "status": None, "rel": rel, "checkpoint": checkpoint}
            return {"git": True, "diff": text + ("\n… diff truncated" if len(text) >= _FILE_VIEW_MAX else ""),
                    "status": code, "rel": rel, "checkpoint": checkpoint, "source": "checkpoint"}
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
        """Open the OS file manager with the file selected (Faustus runs on
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

    @router.post("/open_editor")
    def open_in_editor(
        request: Request,
        workspace: str = Query(default=""),
        path: str = Query(default=""),
        line: int = Query(default=0),
    ):
        """Open the file in VS Code (`code -g file:line`) when it is installed
        on the Faustus host; falls back to the OS default handler."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Admin-only")
        target = _confine(workspace, path)
        if not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="not found")
        import shutil, subprocess, sys as _sys
        code = shutil.which("code") or shutil.which("code.cmd") or shutil.which("codium")
        try:
            if code:
                subprocess.Popen([code, "-g", f"{target}:{max(1, int(line or 1))}"],
                                 shell=os.name == "nt" and code.lower().endswith(".cmd"))
                return {"ok": True, "editor": os.path.basename(code)}
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            elif _sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {"ok": True, "editor": "default"}

    @router.post("/revert")
    def revert_file(
        request: Request,
        workspace: str = Query(default=""),
        path: str = Query(default=""),
        checkpoint: str = Query(default=""),
    ):
        """Undo the changes of one file. With a `checkpoint` sha (the turn's
        shadow snapshot) the file goes back to exactly its pre-turn state —
        works without git. Otherwise: `git checkout -- <file>` for a tracked
        file, delete for an untracked one; refused when the folder is not a
        git repo (no safe baseline to restore)."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Admin-only")
        target = _confine(workspace, path)
        root = os.path.realpath(os.path.expanduser(workspace))
        if checkpoint:
            if not _SHA_RE.match(checkpoint):
                raise HTTPException(status_code=400, detail="bad checkpoint")
            from src import workspace_checkpoints as wc
            rel = os.path.relpath(target, root).replace(os.sep, "/")
            res = wc.restore(root, checkpoint, [rel])
            if res.get("error"):
                raise HTTPException(status_code=400, detail=res["error"])
            if res["failed"]:
                raise HTTPException(status_code=500, detail="restore failed")
            if res["deleted"]:
                return {"ok": True, "action": "deleted_new_file", "checkpoint": checkpoint}
            if res["restored"]:
                return {"ok": True, "action": "restored", "checkpoint": checkpoint}
            return {"ok": True, "action": "unchanged", "checkpoint": checkpoint}
        import subprocess
        try:
            probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=8)
        except (OSError, subprocess.SubprocessError):
            probe = None
        if not probe or probe.returncode != 0:
            raise HTTPException(status_code=400, detail="not a git repository — nothing to revert to (turns started after the checkpoints update can be restored from their checkpoint)")
        top = os.path.realpath(probe.stdout.strip())
        rel = os.path.relpath(target, top).replace(os.sep, "/")
        st = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=top, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=8)
        code = (st.stdout or "")[:2].strip()
        if not code:
            return {"ok": True, "action": "unchanged"}
        if code.startswith("?"):
            try:
                os.remove(target)
            except OSError as e:
                raise HTTPException(status_code=500, detail=str(e))
            return {"ok": True, "action": "deleted_untracked"}
        r = subprocess.run(["git", "checkout", "--", rel], cwd=top, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=15)
        if r.returncode != 0:
            raise HTTPException(status_code=500, detail=(r.stderr or "git checkout failed")[:300])
        return {"ok": True, "action": "restored"}

    # ── Turn checkpoints (src/workspace_checkpoints.py) ───────────────────

    def _admin_only(request: Request) -> None:
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Admin-only")

    def _vetted_root(workspace: str) -> str:
        from src.tool_execution import vet_workspace
        root = vet_workspace(workspace or "")
        if not root:
            raise HTTPException(status_code=400, detail="workspace is not a valid folder")
        return root

    def _check_sha(sha: str) -> str:
        if not sha or not _SHA_RE.match(sha):
            raise HTTPException(status_code=400, detail="bad checkpoint")
        return sha

    @router.get("/checkpoint/status")
    def checkpoint_status(request: Request, workspace: str = Query(default="")):
        _admin_only(request)
        from src import workspace_checkpoints as wc
        return wc.status(_vetted_root(workspace))

    @router.get("/checkpoint/changes")
    def checkpoint_changes(request: Request, workspace: str = Query(default=""), sha: str = Query(default="")):
        """Files that differ NOW from the checkpoint (what "restore" would touch)."""
        _admin_only(request)
        from src import workspace_checkpoints as wc
        root = _vetted_root(workspace)
        return {"sha": _check_sha(sha), "changed": wc.changed_since(root, sha)}

    @router.get("/checkpoint/file")
    def checkpoint_file(request: Request, workspace: str = Query(default=""), sha: str = Query(default=""),
                        path: str = Query(default="")):
        """The file's content at the checkpoint (the 'before' pane of the viewer)."""
        _admin_only(request)
        from src import workspace_checkpoints as wc
        target = _confine(workspace, path)
        root = os.path.realpath(os.path.expanduser(workspace))
        rel = os.path.relpath(target, root).replace(os.sep, "/")
        raw = wc.file_at(root, _check_sha(sha), rel)
        if raw is None:
            return {"exists": False, "rel": rel, "text": "", "binary": False}
        raw = raw[:_FILE_VIEW_MAX]
        binary = b"\x00" in raw[:8000]
        return {"exists": True, "rel": rel, "binary": binary,
                "text": "" if binary else raw.decode("utf-8", errors="replace").replace("\r\n", "\n")}

    @router.post("/checkpoint/restore")
    async def checkpoint_restore(request: Request, workspace: str = Query(default=""), sha: str = Query(default="")):
        """Put files back to their state at the checkpoint. Body: {"paths": [..]}
        restores only those (relative to the workspace); no body / null = every
        file that differs from the checkpoint (the full "before this turn")."""
        _admin_only(request)
        from src import workspace_checkpoints as wc
        root = _vetted_root(workspace)
        _check_sha(sha)
        paths = None
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict) and isinstance(body.get("paths"), list):
            paths = [str(p) for p in body["paths"] if p]
            if not paths:
                return {"ok": True, "restored": [], "deleted": [], "failed": [], "unchanged": 0}
            for p in paths:
                _confine(root, p)   # confinement: every path must resolve inside the workspace
        res = wc.restore(root, sha, paths)
        if res.get("error"):
            raise HTTPException(status_code=400, detail=res["error"])
        res["ok"] = not res["failed"]
        return res

    @router.get("/checkpoint/list")
    def checkpoint_list(request: Request, workspace: str = Query(default=""), limit: int = Query(default=30)):
        _admin_only(request)
        from src import workspace_checkpoints as wc
        return {"checkpoints": wc.list_checkpoints(_vetted_root(workspace), limit=max(1, min(limit, 200)))}

    @router.post("/checkpoint/reset")
    def checkpoint_reset(request: Request, workspace: str = Query(default="")):
        """Delete the shadow repo of this workspace (frees disk, loses old baselines)."""
        _admin_only(request)
        from src import workspace_checkpoints as wc
        return {"ok": wc.reset(_vetted_root(workspace))}

    # ── "Commit these changes" (the user's own git repo) ──────────────────

    @router.get("/commit/proposal")
    def commit_proposal(request: Request, workspace: str = Query(default=""), paths: str = Query(default=""),
                        text: str = Query(default=""), language: str = Query(default="en")):
        _admin_only(request)
        from src import workspace_checkpoints as wc
        root = _vetted_root(workspace)
        files = [p for p in paths.split("\n") if p.strip()] if paths else []
        top = wc.user_repo_root(root)
        return {"git": bool(top), "repo": top, "message": wc.propose_commit_message(text, files, language)}

    @router.post("/commit")
    async def commit_files(request: Request, workspace: str = Query(default="")):
        """Body: {"paths": [...], "message": "..."} → `git add`+`git commit` of
        exactly those paths in the user's repository."""
        _admin_only(request)
        from src import workspace_checkpoints as wc
        root = _vetted_root(workspace)
        try:
            body = await request.json()
        except Exception:
            body = {}
        paths = [str(p) for p in (body.get("paths") or []) if p] if isinstance(body, dict) else []
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
        if not paths:
            raise HTTPException(status_code=400, detail="no paths")
        for p in paths:
            _confine(root, p)
        res = wc.user_git_commit(root, paths, message)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("error") or "commit failed")
        return res

    # ── Review mode: accept / reject per file ─────────────────────────────

    @router.get("/review/{message_id}")
    def review_state(request: Request, message_id: str):
        _admin_only(request)
        from services import review_state as rs
        entry = rs.get(message_id)
        if not entry:
            raise HTTPException(status_code=404, detail="no review state for this message")
        return {"message_id": message_id, **entry}

    @router.post("/review/{message_id}/decide")
    async def review_decide(request: Request, message_id: str):
        """Body: {"path": "...", "decision": "accept"|"reject"}. Reject restores
        the file from the turn's checkpoint (or git when there is none)."""
        _admin_only(request)
        from services import review_state as rs
        from src import workspace_checkpoints as wc
        try:
            body = await request.json()
        except Exception:
            body = {}
        path = str((body or {}).get("path") or "")
        decision = str((body or {}).get("decision") or "")
        if decision not in ("accept", "reject") or not path:
            raise HTTPException(status_code=400, detail="path and decision (accept|reject) required")
        entry = rs.get(message_id)
        if not entry:
            raise HTTPException(status_code=404, detail="no review state for this message")
        if path not in (entry.get("pending") or []) and path not in (entry.get("accepted") or []) and path not in (entry.get("rejected") or []):
            raise HTTPException(status_code=400, detail="that file is not part of this turn")
        root = _vetted_root(entry.get("workspace") or "")
        target = _confine(root, path)
        action = "accepted"
        if decision == "reject":
            rel = os.path.relpath(target, root).replace(os.sep, "/")
            sha = entry.get("checkpoint")
            if sha:
                res = wc.restore(root, sha, [rel])
                if res.get("failed"):
                    raise HTTPException(status_code=500, detail="restore failed")
                action = "deleted_new_file" if res.get("deleted") else ("restored" if res.get("restored") else "unchanged")
            else:
                # No checkpoint (feature was off / git missing): fall back to git.
                top = wc.user_repo_root(root)
                if not top:
                    raise HTTPException(status_code=400, detail="no checkpoint and not a git repository — cannot restore")
                import subprocess
                rel_top = os.path.relpath(target, top).replace(os.sep, "/")
                st = subprocess.run(["git", "status", "--porcelain", "--", rel_top], cwd=top, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=8)
                code = (st.stdout or "")[:2].strip()
                if code.startswith("?"):
                    os.remove(target)
                    action = "deleted_new_file"
                elif code:
                    subprocess.run(["git", "checkout", "--", rel_top], cwd=top, capture_output=True, timeout=15)
                    action = "restored"
                else:
                    action = "unchanged"
        updated = rs.decide(message_id, path, decision)
        return {"ok": True, "action": action, "state": updated}

    return router

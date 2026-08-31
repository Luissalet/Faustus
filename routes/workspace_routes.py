"""Workspace API - browse server directories to pick a tool workspace folder."""
import os
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, HTTPException, Query

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user

# Cap entries returned per directory (mirrors filesystem_tools._CODENAV_MAX_HITS).
# A huge directory shouldn't dump thousands of rows into the picker; the user can
# type/paste a path to jump straight in instead.
_MAX_BROWSE_DIRS = 500

# Cap the `paths` list a mutating body may carry. Each entry costs a realpath()
# plus a place in a `git diff`/`checkout` argv, so an unbounded list is a cheap
# way to burn the box (500k entries = 500k realpath calls + an argv that blows
# past the OS command-line limit). Nothing in the UI ever sends more than the
# files one turn touched.
_MAX_BODY_PATHS = 2000


# ── CSRF / cross-origin guard for the mutating POSTs ──────────────────────
#
# Faustus is a local single-user app (127.0.0.1:7000) that normally runs with
# AUTH_ENABLED=false, so owner_is_admin_or_single_user() is always True and the
# `_admin_only` checks are no-ops. There is no CSRF token anywhere, and
# CORSMiddleware does NOT stop a cross-origin request from *executing* — it only
# hides the response from the calling page. Worse, Starlette's Request.json()
# ignores Content-Type, so a plain
#     fetch(url, {method:'POST', mode:'no-cors',
#                 headers:{'Content-Type':'text/plain'}, body:'{"paths":[...]}'})
# from ANY page the user happens to have open is a "simple request" (no
# preflight) that reaches these handlers and deletes files, rewrites AGENTS.md
# or throws away every checkpoint. The attacker never needs to read the answer.
#
# Every browser that can mount that attack also sends Sec-Fetch-Site (Chrome 76+,
# Firefox 90+, Safari 16.4+) and cannot forge it: it is a forbidden header name.
# The app's own fetches are `same-origin`; a foreign page gets `cross-site`, and
# `same-site` for a different port/subdomain of the same registrable domain —
# which for a localhost app is still Not Us. So: allow `same-origin` only.
#
# Non-browser callers (curl, the backend's own loopback, an HTTP client in a
# script) send neither Sec-Fetch-Site nor Origin, and those must keep working —
# they are not the threat, because a web page cannot make the browser omit the
# header. Absent-and-absent is therefore allowed on purpose.
_ALLOWED_FETCH_SITES = frozenset({"same-origin"})


def _reject_cross_origin(request: Request) -> None:
    """403 unless this POST plausibly came from the app's own page (or a CLI).

    Accepted:
      * no Sec-Fetch-Site and no Origin  → curl / backend loopback / old client
      * Sec-Fetch-Site: same-origin      → the Faustus UI itself
    Rejected:
      * Sec-Fetch-Site: cross-site / same-site / none → another page's request
      * an Origin whose host is not this request's Host → cross-origin write
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site and site not in _ALLOWED_FETCH_SITES:
        raise HTTPException(
            status_code=403,
            detail=("Cross-origin request rejected (Sec-Fetch-Site: "
                    f"{site}). This endpoint changes files on disk and may only "
                    "be called from the Faustus page itself."),
        )

    origin = (request.headers.get("origin") or "").strip()
    if origin:
        host = (request.headers.get("host") or "").strip().lower()
        origin_host = urlsplit(origin).netloc.lower()
        # "null" (sandboxed iframe, file://) has no netloc and never matches.
        if not origin_host or not host or origin_host != host:
            raise HTTPException(
                status_code=403,
                detail=(f"Cross-origin request rejected (Origin {origin!r} does "
                        f"not match Host {host!r}). This endpoint changes files "
                        "on disk and may only be called from the Faustus page itself."),
            )


# ── git: the repo being viewed must not choose what runs ──────────────────
#
# These routes run git in a `cwd` the client names, inheriting the full server
# environment. Git happily executes commands a *repository* configures:
#   [diff] external = <cmd>     → runs on `git diff` (silently, output replaced)
#   [core] fsmonitor = <cmd>    → runs on `git status`
#   GIT_EXTERNAL_DIFF=<cmd>     → same, from the environment
# so opening the diff viewer on a folder the agent (or a cloned repo) prepared
# was code execution. `--no-ext-diff` kills both the config and the env var,
# `-c core.fsmonitor=` kills the status hook, and `_git_env()` (reused from
# src/workspace_checkpoints.py, which already had to solve exactly this) strips
# GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE so an inherited variable cannot
# redirect the command at another repository.
_GIT_HARDENING = ("-c", "core.fsmonitor=", "--no-pager")


def _git_env() -> dict:
    """The cleaned environment src/workspace_checkpoints.py already builds."""
    try:
        from src.workspace_checkpoints import _git_env as _wc_git_env
        return _wc_git_env()
    except Exception:                                  # pragma: no cover
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(var, None)
        return env


def _git_argv(*args: str) -> list:
    """`git` argv with the hardening flags that must precede the subcommand."""
    return ["git", *_GIT_HARDENING, *args]


# ── launching the editor without handing cmd.exe a filename to parse ──────
#
# The bug this replaces:
#     subprocess.Popen([code, "-g", f"{target}:{line}"],
#                      shell=os.name == "nt" and code.lower().endswith(".cmd"))
# With shell=True on Windows, Popen does NOT hand CreateProcess an argv: it
# joins the list with subprocess.list2cmdline() and feeds the string to
# `cmd.exe /c`. list2cmdline only quotes an argument that contains a space or a
# tab, so `&  |  ^  <  >  (  )` go through raw and cmd.exe reads them as its own
# syntax. A file called `x&calc.exe` — a perfectly legal NTFS name, and one the
# agent itself can create — turns "open this file" into "open x, then run
# calc.exe". `shell=True` is gone for good.
#
# Removing it is not enough on its own: CreateProcess cannot execute a .cmd/.bat
# (it is a script, not a PE image), and `shutil.which("code")` on Windows
# usually resolves to VS Code's bin\code.cmd. Two ways out, and we take both in
# this order:
#
#   1. PREFER A REAL EXECUTABLE. `code.exe`/`codium.exe` needs no interpreter,
#      so Popen passes the argv to CreateProcess and *nothing* parses the
#      filename. This is the only fully metacharacter-free path, so we look for
#      it first instead of accepting whatever `which` returns.
#
#   2. ONLY IF a .cmd/.bat is all there is, go through cmd.exe explicitly, with
#      the command line built HERE rather than by list2cmdline:
#          cmd.exe /d /v:off /s /c ""<code.cmd>" "-g" "<file>:<line>""
#      Each token carries its own quotes, so cmd treats `&` and friends as
#      literal text; /s makes cmd strip only the outer quote pair and leave the
#      rest verbatim; /d skips the AutoRun registry command; /v:off keeps `!`
#      inert. It is passed to Popen as a STRING (Windows accepts a raw command
#      line) precisely so list2cmdline does not re-mangle those quotes — and
#      still with shell=False, so no second shell ever sees it.
#
# A `"` would be the one character this quoting cannot survive; it is illegal in
# a Windows filename, so it can only mean something is wrong — we refuse the cmd
# route and let the caller fall back to the OS default handler.
_CMD_SCRIPT_SUFFIXES = (".cmd", ".bat")


def _editor_launch_args(code: str, target: str, line: int, *, windows: bool):
    """What to hand subprocess.Popen to open `target` at `line` in `code`.

    Returns a list (argv, executed directly — no shell, no cmd) or a str (a
    fully quoted `cmd.exe /d /v:off /s /c` command line for a .cmd/.bat), or
    None when it cannot be built safely. Never returns anything that lets a
    character in `target` become a command separator.
    """
    spec = f"{target}:{max(1, int(line or 1))}"
    if not windows or not code.lower().endswith(_CMD_SCRIPT_SUFFIXES):
        # Real executable → CreateProcess/execve gets the argv verbatim.
        return [code, "-g", spec]
    if '"' in code or '"' in spec:
        return None
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    if '"' in comspec:
        return None
    inner = " ".join(f'"{part}"' for part in (code, "-g", spec))
    return f'"{comspec}" /d /v:off /s /c "{inner}"'


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
        # The cap has to bite DURING the scan, not after it. The old code
        # collected (and stat()ed) every entry of the directory and only then
        # sliced to 500 — so pointing the picker at a folder with a million
        # files cost a million stat() calls and a million dicts before a single
        # byte went out. Stop as soon as both lists are full; `truncated` says
        # the listing is partial, which is all the UI does with it anyway.
        truncated = False
        try:
            with os.scandir(target) as it:
                for entry in it:
                    if len(dirs) >= _MAX_BROWSE_DIRS and (
                            not include_files or len(files) >= _MAX_BROWSE_DIRS):
                        truncated = True
                        break
                    try:
                        # Don't follow symlinks when classifying - a symlinked
                        # dir is skipped rather than letting the browser wander
                        # off via a link. Hidden entries are omitted.
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                            if len(dirs) >= _MAX_BROWSE_DIRS:
                                truncated = True
                                continue
                            # Build the child path server-side with os.path.join
                            # so it's correct on Windows (backslashes) and Linux.
                            dirs.append({"name": entry.name, "path": os.path.join(target, entry.name)})
                        elif include_files and entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                            if len(files) >= _MAX_BROWSE_DIRS:
                                truncated = True
                                continue
                            files.append({
                                "name": entry.name,
                                "path": os.path.join(target, entry.name),
                                # stat() only for entries we are actually going
                                # to return.
                                "size": entry.stat(follow_symlinks=False).st_size,
                            })
                    except OSError:
                        continue
        except (PermissionError, OSError):
            dirs = []

        parent = os.path.dirname(target)
        from src.tool_execution import vet_workspace
        return {
            "path": target,
            "parent": parent if parent and parent != target else None,
            "dirs": sorted(dirs, key=lambda d: d["name"].lower()),
            "files": sorted(files, key=lambda f: f["name"].lower()),
            # True when either list hit the cap — dirs OR files.
            "truncated": truncated,
            # Whether this directory may be bound as a workspace (filesystem
            # roots and sensitive dirs may be browsed through but not chosen).
            "selectable": vet_workspace(target) is not None,
        }

    @router.get("/files")
    def files(
        request: Request,
        workspace: str = Query(default=""),
        q: str = Query(default=""),
        limit: int = Query(default=12),
    ):
        """Ranked workspace files for the composer's `@` picker.

        ADMIN-ONLY for the same reason as /browse: it enumerates paths on the
        host. Reads the cached workspace index, so a keystroke costs a sort,
        not a walk.
        """
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace browsing is admin-only")
        from src.tool_execution import vet_workspace
        root = vet_workspace(workspace or "")
        if not root:
            return {"files": [], "workspace": "", "error": "workspace is not a valid folder"}
        from src import file_mentions
        rows = file_mentions.search(root, q, limit=min(max(int(limit or 12), 1), 50))
        return {"workspace": root, "q": q, "files": rows}

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
        # Hardened argv + cleaned env everywhere below: `root` is a folder the
        # client names, so its .git/config is attacker-controlled data as far as
        # this process is concerned. See _git_argv / _git_env above.
        env = _git_env()
        try:
            probe = subprocess.run(_git_argv("rev-parse", "--show-toplevel"), cwd=root,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=8, env=env)
        except (OSError, subprocess.SubprocessError):
            probe = None
        if not probe or probe.returncode != 0:
            return {"git": False, "diff": "", "status": None}
        top = os.path.realpath(probe.stdout.strip())
        rel = os.path.relpath(target, top).replace(os.sep, "/")
        try:
            st = subprocess.run(_git_argv("status", "--porcelain", "--", rel), cwd=top,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=8, env=env)
            code = (st.stdout or "")[:2].strip() or None
            if code and code.startswith("?"):
                diff = subprocess.run(_git_argv("diff", "--no-color", "--no-ext-diff", "--no-index", "--",
                                                "/dev/null" if os.name != "nt" else "NUL", rel),
                                      cwd=top, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=8, env=env)
            else:
                diff = subprocess.run(_git_argv("diff", "--no-color", "--no-ext-diff", "--", rel),
                                      cwd=top, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=8, env=env)
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
        _reject_cross_origin(request)
        target = _confine(workspace, path)
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail="not found")
        import subprocess, sys as _sys
        try:
            if os.name == "nt":
                # No `shell=` here (and there never was): explorer/open/xdg-open
                # are real executables, so Popen hands CreateProcess/execve the
                # argv and nothing re-parses `target`. A file called
                # `x&calc.exe` is one argument, not two commands.
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
        _reject_cross_origin(request)
        target = _confine(workspace, path)
        if not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="not found")
        import shutil, subprocess, sys as _sys
        # Look for a real executable BEFORE the .cmd wrapper: an .exe runs
        # through CreateProcess with no interpreter in the way, which is the
        # only launch path where a `&` in the filename can never be syntax.
        code = (shutil.which("code.exe") or shutil.which("codium.exe")
                or shutil.which("code") or shutil.which("code.cmd")
                or shutil.which("codium"))
        try:
            launch = _editor_launch_args(code, target, line, windows=os.name == "nt") if code else None
            if launch is not None:
                # shell=False always — see _editor_launch_args for why the .cmd
                # case is a pre-quoted command-line string instead of a list.
                subprocess.Popen(launch, shell=False)
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
        _reject_cross_origin(request)
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
        env = _git_env()
        try:
            probe = subprocess.run(_git_argv("rev-parse", "--show-toplevel"), cwd=root, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=8, env=env)
        except (OSError, subprocess.SubprocessError):
            probe = None
        if not probe or probe.returncode != 0:
            raise HTTPException(status_code=400, detail="not a git repository — nothing to revert to (turns started after the checkpoints update can be restored from their checkpoint)")
        top = os.path.realpath(probe.stdout.strip())
        rel = os.path.relpath(target, top).replace(os.sep, "/")
        st = subprocess.run(_git_argv("status", "--porcelain", "--", rel), cwd=top, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=8, env=env)
        code = (st.stdout or "")[:2].strip()
        if not code:
            return {"ok": True, "action": "unchanged"}
        if code.startswith("?"):
            try:
                os.remove(target)
            except OSError as e:
                raise HTTPException(status_code=500, detail=str(e))
            return {"ok": True, "action": "deleted_untracked"}
        r = subprocess.run(_git_argv("checkout", "--", rel), cwd=top, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=15, env=env)
        if r.returncode != 0:
            raise HTTPException(status_code=500, detail=(r.stderr or "git checkout failed")[:300])
        return {"ok": True, "action": "restored"}

    # ── Turn checkpoints (src/workspace_checkpoints.py) ───────────────────

    def _admin_only(request: Request) -> None:
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Admin-only")

    def _admin_only_write(request: Request) -> None:
        """Gate for the POSTs that change files on disk: admin AND same-origin.

        Split out from `_admin_only` on purpose — the read routes stay reachable
        from a CLI/script that sends no Origin, while every destructive verb has
        to look like it came from the Faustus page (see _reject_cross_origin)."""
        _admin_only(request)
        _reject_cross_origin(request)

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

    def _confined_body_paths(root: str, raw) -> list:
        """The `paths` list of a mutating body, bounded and confined.

        Two bugs in one: the list had no cap (500k entries = 500k realpath()
        calls, then an argv `git diff`/`checkout` cannot even hold), and the
        callers validated one list and passed a DIFFERENT one downstream — every
        path went through _confine() and then the RAW strings were handed to
        wc.restore()/wc.user_git_commit(), so the confinement decided nothing.
        This returns the resolved, confined paths, which is what must be used."""
        items = [str(p) for p in (raw or []) if p]
        if len(items) > _MAX_BODY_PATHS:
            raise HTTPException(
                status_code=400,
                detail=f"too many paths: {len(items)} (max {_MAX_BODY_PATHS})")
        return [_confine(root, p) for p in items]

    # ── AGENTS.md draft (project instructions the runtime injects) ────────
    @router.post("/instructions/draft")
    async def instructions_draft(request: Request):
        """Body: {"workspace": "...", "write": bool, "language": "en"|"es"}.
        Returns the draft; with write=true it is saved as AGENTS.md when no
        instructions file exists yet (never overwrites)."""
        _admin_only_write(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        root = _vetted_root(str(body.get("workspace") or ""))
        from src import project_instructions as pi
        d = pi.draft(root, language=str(body.get("language") or "en"))
        if d.get("error"):
            raise HTTPException(status_code=400, detail=d["error"])
        d["written"] = False
        if body.get("write"):
            if d.get("exists"):
                d["note"] = "an instructions file already exists; nothing written"
            else:
                try:
                    with open(d["path"], "w", encoding="utf-8", newline="\n") as f:
                        f.write(d["text"])
                    d["written"] = True
                    pi.invalidate(root)
                except OSError as e:
                    raise HTTPException(status_code=500, detail=f"could not write AGENTS.md: {e}")
        return d

    @router.post("/instructions/remember")
    async def instructions_remember(request: Request):
        """Body: {"workspace": "...", "text": "..."} — append one standing rule
        to the project's instructions file (Claude Code's "#" shortcut).

        Creates AGENTS.md when the project has none; never rewrites existing
        content, and a rule already in the file is reported as a duplicate
        rather than added twice."""
        _admin_only_write(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        root = _vetted_root(str(body.get("workspace") or ""))
        from src import project_instructions as pi
        res = pi.remember(root, str(body.get("text") or ""))
        if res.get("error"):
            raise HTTPException(status_code=400, detail=res["error"])
        return res

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
        _admin_only_write(request)
        from src import workspace_checkpoints as wc
        root = _vetted_root(workspace)
        _check_sha(sha)
        paths = None
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict) and isinstance(body.get("paths"), list):
            if not [p for p in body["paths"] if p]:
                return {"ok": True, "restored": [], "deleted": [], "failed": [], "unchanged": 0}
            # Confined absolute paths, and it is THESE that get restored.
            paths = _confined_body_paths(root, body["paths"])
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
        _admin_only_write(request)
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
        _admin_only_write(request)
        from src import workspace_checkpoints as wc
        root = _vetted_root(workspace)
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_paths = (body.get("paths") or []) if isinstance(body, dict) else []
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
        if not [p for p in raw_paths if p]:
            raise HTTPException(status_code=400, detail="no paths")
        # Confined absolute paths, and it is THESE that get committed.
        paths = _confined_body_paths(root, raw_paths)
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
        _admin_only_write(request)
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
                env = _git_env()
                st = subprocess.run(_git_argv("status", "--porcelain", "--", rel_top), cwd=top,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=8, env=env)
                code = (st.stdout or "")[:2].strip()
                if code.startswith("?"):
                    os.remove(target)
                    action = "deleted_new_file"
                elif code:
                    subprocess.run(_git_argv("checkout", "--", rel_top), cwd=top,
                                   capture_output=True, timeout=15, env=env)
                    action = "restored"
                else:
                    action = "unchanged"
        updated = rs.decide(message_id, path, decision)
        return {"ok": True, "action": action, "state": updated}

    return router
